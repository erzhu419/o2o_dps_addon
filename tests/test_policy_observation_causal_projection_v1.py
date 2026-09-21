from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace
import unittest

from o2o_dps.cat2new_candidate_feedback_loop_v6 import POLICY_INPUT_SCHEMA_V6
from o2o_dps.cat2new_fury_parametric_policy_v1 import (
    Cat2NewFuryParametricPolicyV1,
)
from o2o_dps.expert_policy import SwingQueueOp
from o2o_dps.expert_proposals import ACTION_KEY_TO_REF, QUEUE_REFS
from o2o_dps.fury_paired_multiseed_runner_v4 import CAT2NEW_POLICY_ID
from o2o_dps.historical_behavior_clone_full_rollout_v2 import (
    _observation_factory as clone_observation_factory,
)
from o2o_dps.policy_observation_causal_projection_v1 import (
    PolicyObservationCausalProjectionV1Error,
    TargetHealthPrefixBaselineV1,
    TargetHealthPrefixRegistryV1,
    TargetIntroductionRegistryV1,
    TargetIntroductionV1,
    canonical_policy_observation_bytes_v1,
    project_cat2_policy_input_v1,
    project_live_state_for_policy_v1,
)
from tests.test_sim_bridge_dynamic_v4 import bound_state_v4, config_v4


def _health_registry(
    rows: tuple[tuple[int, int, float, float], ...],
) -> TargetHealthPrefixRegistryV1:
    return TargetHealthPrefixRegistryV1(
        targets=tuple(
            TargetHealthPrefixBaselineV1(
                simulator_target_index=index,
                observed_at_ms=observed_at_ms,
                current_health=current,
                maximum_health=maximum,
                simulated_damage_applied_at_observation=0.0,
                background_damage_applied_at_observation=0.0,
            )
            for index, observed_at_ms, current, maximum in rows
        )
    )


def _target(
    index: int,
    *,
    health: float,
    maximum: float,
    armor: float,
    attackable: bool = True,
) -> tuple[dict[str, object], dict[str, object]]:
    dead = health <= 0
    life = {
        "target_index": index,
        "initial_health": maximum,
        "current_health": health,
        "dead": dead,
        "simulated_damage_applied": maximum - health,
        "background_damage_applied": 0.0,
    }
    semantics = {
        "target_index": index,
        "attackable": attackable and not dead,
        "effective_armor": armor,
        "current_health": health,
        "dead": dead,
    }
    if dead:
        life["death_time_ms"] = 90
        semantics["death_time_ms"] = 90
    return life, semantics


def _state(
    future_targets: list[tuple[float, float, float, bool]],
    *,
    config_digest: str,
    environment_generation: int,
) -> dict[str, object]:
    rows = [_target(0, health=800.0, maximum=1000.0, armor=1104.0)]
    rows.extend(
        _target(
            index,
            health=health,
            maximum=maximum,
            armor=armor,
            attackable=attackable,
        )
        for index, (health, maximum, armor, attackable) in enumerate(
            future_targets, start=1
        )
    )
    life_rows = [row[0] for row in rows]
    semantic_rows = [row[1] for row in rows]
    background = sum(float(row["background_damage_applied"]) for row in life_rows)
    simulated = sum(float(row["simulated_damage_applied"]) for row in life_rows)
    return {
        "time_ms": 100,
        "finished": False,
        "needs_input": True,
        "target_index": 0,
        "num_targets": sum(
            row["attackable"] is True and row["dead"] is False
            for row in semantic_rows
        ),
        "total_target_count": len(rows),
        "target_health_known": True,
        "target_health": 800.0,
        "target_health_max": 1000.0,
        "target_health_percent": 80.0,
        "target_armor": 1104.0,
        "effective_target_armor": 974.0,
        "encounter_health_target": sum(float(row["initial_health"]) for row in life_rows),
        "encounter_damage_taken": simulated + background,
        "remaining_ms": 900_000 - environment_generation,
        "execute_phase_20": False,
        "execute_phase_25": False,
        "execute_phase_35": False,
        "power": {"current": 60.0, "maximum": 100.0, "type": "rage"},
        "gcd_remaining_ms": 0,
        "mh_swing_duration_ms": 3200,
        "mh_swing_remaining_ms": 1200,
        "oh_swing_remaining_ms": 700,
        "queued_swing": "KEEP",
        "moving": False,
        "armor_penetration": 130,
        "damage_done": 200.0,
        "health_current": 4829.0,
        "health_maximum": 4829.0,
        "melee_attack_power": 1502.0,
        "strength": 387.0,
        "auras": [
            {
                "action": {"spell_id": 2458},
                "label": "Berserker Stance",
                "remaining_ms": -1,
                "stacks": 0,
            },
            {
                "action": {"spell_id": 25289},
                "label": "Battle Shout",
                "remaining_ms": 60_000,
                "stacks": 0,
            },
            {
                "action": {},
                "label": "Flurry Proc Trigger",
                "remaining_ms": -1,
                "stacks": 0,
            },
        ],
        "target_auras": [],
        "dynamic_idle_advance": {
            "schema": "o2o_dynamic_idle_advance_receipts/v3",
            "config_digest": config_digest,
            "environment_generation": environment_generation,
            "mode": "CENTRAL_TO_NEXT_ATTACKABLE_OR_HORIZON",
            "horizon_ms": 800_000 + environment_generation,
            "active": False,
            "receipts_processed": 0,
            "total_auto_advanced_ms": 0,
            "stream_closed": False,
            "planned_wake_time_ms": 500 + environment_generation,
            "planned_wake_source": "NEXT_ATTACKABILITY_TRUE",
        },
        "dynamic_team_background": {
            "schema": "o2o_dynamic_target_semantics/v3",
            "config_digest": config_digest,
            "environment_generation": environment_generation,
            "same_timestamp_order": "suffix-specific-order",
            "retarget_mode": "NEXT_ALIVE_CYCLIC",
            "retarget_required": False,
            "simulated_damage_applied": simulated,
            "background_damage_applied": background,
            "combined_damage_applied": simulated + background,
            "background_events_processed": 3,
            "background_events_total": 1000 + environment_generation,
            "background_events_canceled": 0,
            "background_damage_applications_processed": 3,
            "candidate_events_processed": 1,
            "candidate_events_canceled": 0,
            "damage_applications_total": 4,
            "targets": life_rows,
        },
        "dynamic_target_semantics": {
            "schema": "o2o_dynamic_target_semantics/v3",
            "config_digest": config_digest,
            "environment_generation": environment_generation,
            "same_timestamp_order": "suffix-specific-order",
            "attackability_events_processed": 1,
            "attackability_events_total": 10 + environment_generation,
            "effective_armor_events_processed": 1,
            "effective_armor_events_total": 20 + environment_generation,
            "targets": semantic_rows,
        },
    }


def _available_actions() -> list[dict[str, object]]:
    specs = [
        (ACTION_KEY_TO_REF["warrior.bloodthirst"], "Bloodthirst", True),
        (ACTION_KEY_TO_REF["warrior.whirlwind"], "Whirlwind", True),
        (ACTION_KEY_TO_REF["warrior.bloodrage"], "Bloodrage", False),
        (ACTION_KEY_TO_REF["warrior.slam"], "Slam", True),
        (ACTION_KEY_TO_REF["warrior.execute"], "Execute", True),
        (ACTION_KEY_TO_REF["warrior.battle_shout"], "Battle Shout", True),
        (QUEUE_REFS[SwingQueueOp.HEROIC_STRIKE], "Heroic Strike", False),
        (QUEUE_REFS[SwingQueueOp.CLEAVE], "Cleave", False),
    ]
    return [
        {
            "index": index,
            "action": ref.to_wire(),
            "label": label,
            "legal": True,
            "ready_in_ms": 0,
            "triggers_gcd": triggers_gcd,
        }
        for index, (ref, label, triggers_gcd) in enumerate(specs)
    ]


def _raw_boundary(state: dict[str, object]) -> dict[str, object]:
    semantics = state["dynamic_target_semantics"]
    lifecycle = state["dynamic_team_background"]
    assert isinstance(semantics, dict)
    assert isinstance(lifecycle, dict)
    return {
        "target_index": 0,
        "retarget_required": False,
        "selected_target_semantics": deepcopy(semantics["targets"][0]),
        "selected_target_lifecycle": deepcopy(lifecycle["targets"][0]),
        "target_semantics_rows": deepcopy(semantics["targets"]),
        "target_lifecycle_rows": deepcopy(lifecycle["targets"]),
    }


def _policy_input(state: dict[str, object], *, run_digest: str) -> dict[str, object]:
    return {
        "schema": POLICY_INPUT_SCHEMA_V6,
        "policy_id": CAT2NEW_POLICY_ID,
        "decision_index": 1,
        "run_binding_content_sha256": run_digest,
        "live_state": state,
        "available_actions": _available_actions(),
        "pending_attempt_ids": [],
        "target_boundary": _raw_boundary(state),
        "optimizer_parameters": {},
        "historical_prior": {},
    }


class PolicyObservationCausalProjectionV1Tests(unittest.TestCase):
    def test_available_action_runtime_semantics_are_causal_and_legacy_safe(self):
        state = _state(
            [],
            config_digest="a" * 64,
            environment_generation=11,
        )
        raw = _policy_input(state, run_digest="b" * 64)
        raw["available_actions"][0]["cooldown_duration_ms"] = 6000
        raw["available_actions"][0]["result_bearing"] = True
        projection = project_cat2_policy_input_v1(
            raw,
            TargetIntroductionRegistryV1(
                targets=(TargetIntroductionV1(0, 0),)
            ),
            _health_registry(((0, 0, 1000.0, 1000.0),)),
        )
        first = projection.policy_input["available_actions"][0]
        second = projection.policy_input["available_actions"][1]
        self.assertEqual(6000, first["cooldown_duration_ms"])
        self.assertTrue(first["result_bearing"])
        self.assertEqual(0, second["cooldown_duration_ms"])
        self.assertFalse(second["result_bearing"])

    def test_common_prefix_different_future_has_identical_bytes_and_cat2_decision(self):
        left_state = _state(
            [(9000.0, 9000.0, 5000.0, False)],
            config_digest="a" * 64,
            environment_generation=11,
        )
        right_state = _state(
            [
                (17.0, 7777.0, 1.0, False),
                (500_000.0, 500_000.0, 9999.0, False),
            ],
            config_digest="b" * 64,
            environment_generation=29,
        )
        left_registry = TargetIntroductionRegistryV1(
            targets=(TargetIntroductionV1(0, 0), TargetIntroductionV1(1, 500))
        )
        right_registry = TargetIntroductionRegistryV1(
            targets=(
                TargetIntroductionV1(0, 0),
                TargetIntroductionV1(1, 700),
                TargetIntroductionV1(2, 900),
            )
        )

        left = project_cat2_policy_input_v1(
            _policy_input(left_state, run_digest="1" * 64),
            left_registry,
            _health_registry(((0, 0, 1000.0, 1000.0), (1, 500, 9000.0, 9000.0))),
        )
        right = project_cat2_policy_input_v1(
            _policy_input(right_state, run_digest="2" * 64),
            right_registry,
            _health_registry(
                (
                    (0, 0, 1000.0, 1000.0),
                    (1, 700, 7777.0, 7777.0),
                    (2, 900, 500_000.0, 500_000.0),
                )
            ),
        )

        self.assertEqual(
            canonical_policy_observation_bytes_v1(left.policy_input),
            canonical_policy_observation_bytes_v1(right.policy_input),
        )
        self.assertEqual((0,), left.policy_to_simulator_target_index)
        self.assertEqual((0,), right.policy_to_simulator_target_index)
        state = left.policy_input["live_state"]
        self.assertEqual(1, state["num_targets"])
        self.assertEqual(1, state["total_target_count"])
        self.assertNotIn("dynamic_idle_advance", state)
        self.assertNotIn("remaining_ms", state)
        self.assertNotIn("run_binding_content_sha256", left.policy_input)
        self.assertNotIn(
            "background_events_total", state["dynamic_team_background"]
        )
        self.assertNotIn(
            "attackability_events_total", state["dynamic_target_semantics"]
        )
        rate = state["dynamic_team_background"]["prefix_damage_rate"]
        self.assertEqual("CURRENT_PREFIX_DAMAGE_DELTAS_ONLY", rate["source_semantics"])
        self.assertEqual(100, rate["elapsed_ms"])
        self.assertEqual(200.0, rate["combined_damage"])
        self.assertEqual(2000.0, rate["combined_damage_per_second"])
        serialized = json.dumps(left.policy_input, sort_keys=True)
        self.assertNotIn("9000.0", serialized)
        self.assertNotIn("500000.0", serialized)

        left_decision = Cat2NewFuryParametricPolicyV1().decide(left.policy_input)
        right_decision = Cat2NewFuryParametricPolicyV1().decide(right.policy_input)
        self.assertEqual(left_decision.to_wire(), right_decision.to_wire())

        clone_factory = clone_observation_factory(
            root_time_ms=0, last_gcd={}
        )
        clone_adapter = SimpleNamespace(
            runtime=SimpleNamespace(last_action_key=None)
        )
        left_clone_observation = clone_factory(
            left.policy_input["live_state"], (), clone_adapter
        )
        right_clone_observation = clone_factory(
            right.policy_input["live_state"], (), clone_adapter
        )
        self.assertEqual(left_clone_observation, right_clone_observation)
        self.assertEqual(1, left_clone_observation["observed_target_count"])

    def test_control_context_and_untyped_action_metadata_cannot_leak_suffix(self):
        state = _state([], config_digest="7" * 64, environment_generation=33)
        registry = TargetIntroductionRegistryV1(
            targets=(TargetIntroductionV1(0, 0),)
        )
        health = _health_registry(((0, 0, 1000.0, 1000.0),))
        left_input = _policy_input(state, run_digest="1" * 64)
        right_input = deepcopy(left_input)
        left_input["optimizer_parameters"] = {
            "future_damage_schedule": [{"time_ms": 900, "damage": 1.0}]
        }
        right_input["optimizer_parameters"] = {
            "future_damage_schedule": [{"time_ms": 900, "damage": 999999.0}]
        }
        left_input["historical_prior"] = {"future_target_hp": 10.0}
        right_input["historical_prior"] = {"future_target_hp": 900000.0}

        left = project_cat2_policy_input_v1(left_input, registry, health)
        right = project_cat2_policy_input_v1(right_input, registry, health)

        self.assertEqual({}, left.policy_input["optimizer_parameters"])
        self.assertEqual({}, left.policy_input["historical_prior"])
        self.assertEqual(
            canonical_policy_observation_bytes_v1(left.policy_input),
            canonical_policy_observation_bytes_v1(right.policy_input),
        )

        poisoned = deepcopy(left_input)
        poisoned["available_actions"][0]["future_schedule"] = [999999]
        with self.assertRaisesRegex(
            PolicyObservationCausalProjectionV1Error,
            "unclassified fields: future_schedule",
        ):
            project_cat2_policy_input_v1(poisoned, registry, health)

    def test_not_yet_introduced_raw_attackable_target_fails_closed(self):
        state = _state(
            [(9000.0, 9000.0, 5000.0, True)],
            config_digest="9" * 64,
            environment_generation=31,
        )
        registry = TargetIntroductionRegistryV1(
            targets=(TargetIntroductionV1(0, 0), TargetIntroductionV1(1, 500))
        )

        with self.assertRaisesRegex(
            PolicyObservationCausalProjectionV1Error,
            "not-yet-introduced target must be raw-unattackable",
        ):
            project_live_state_for_policy_v1(
                state,
                registry,
                _health_registry(
                    ((0, 0, 1000.0, 1000.0), (1, 500, 9000.0, 9000.0))
                ),
            )

    def test_noncontiguous_simulator_visibility_is_compacted_for_policy(self):
        state = _state(
            [
                (9000.0, 9000.0, 5000.0, False),
                (700.0, 700.0, 2200.0, True),
            ],
            config_digest="c" * 64,
            environment_generation=7,
        )
        state["target_index"] = 2
        state["target_health"] = 700.0
        state["target_health_max"] = 700.0
        state["target_health_percent"] = 100.0
        state["target_armor"] = 2200.0
        registry = TargetIntroductionRegistryV1(
            targets=(
                TargetIntroductionV1(0, 0),
                TargetIntroductionV1(1, 500),
                TargetIntroductionV1(2, 50),
            )
        )

        projected = project_live_state_for_policy_v1(
            state,
            registry,
            _health_registry(
                (
                    (0, 0, 1000.0, 1000.0),
                    (1, 500, 9000.0, 9000.0),
                    (2, 50, 700.0, 700.0),
                )
            ),
        )

        self.assertEqual((0, 2), projected.policy_to_simulator_target_index)
        self.assertEqual(1, projected.state["target_index"])
        self.assertEqual(2, projected.state["total_target_count"])
        self.assertEqual(2, projected.simulator_target_index(1))
        rows = projected.state["dynamic_target_semantics"]["targets"]
        self.assertEqual([0, 1], [row["target_index"] for row in rows])

    def test_absent_or_partial_introduction_contract_fails_closed(self):
        state = _state(
            [(9000.0, 9000.0, 5000.0, True)],
            config_digest="d" * 64,
            environment_generation=3,
        )
        with self.assertRaisesRegex(
            PolicyObservationCausalProjectionV1Error,
            "explicit TargetIntroductionRegistryV1",
        ):
            project_live_state_for_policy_v1(state, None, None)

        partial = TargetIntroductionRegistryV1(
            targets=(TargetIntroductionV1(0, 0),)
        )
        with self.assertRaisesRegex(
            PolicyObservationCausalProjectionV1Error,
            "does not exactly cover",
        ):
            project_live_state_for_policy_v1(
                state,
                partial,
                _health_registry(
                    ((0, 0, 1000.0, 1000.0), (1, 500, 9000.0, 9000.0))
                ),
            )

    def test_selected_future_target_fails_closed(self):
        state = _state(
            [(9000.0, 9000.0, 5000.0, True)],
            config_digest="e" * 64,
            environment_generation=4,
        )
        state["target_index"] = 1
        registry = TargetIntroductionRegistryV1(
            targets=(TargetIntroductionV1(0, 0), TargetIntroductionV1(1, 500))
        )
        with self.assertRaisesRegex(
            PolicyObservationCausalProjectionV1Error,
            "selected simulator target is not prefix-visible",
        ):
            project_live_state_for_policy_v1(
                state,
                registry,
                _health_registry(
                    ((0, 0, 1000.0, 1000.0), (1, 500, 9000.0, 9000.0))
                ),
            )

    def test_unclassified_live_state_field_fails_closed(self):
        state = _state(
            [(9000.0, 9000.0, 5000.0, True)],
            config_digest="f" * 64,
            environment_generation=5,
        )
        state["future_team_schedule"] = [{"time_ms": 600, "damage": 99999}]
        registry = TargetIntroductionRegistryV1(
            targets=(TargetIntroductionV1(0, 0), TargetIntroductionV1(1, 500))
        )
        with self.assertRaisesRegex(
            PolicyObservationCausalProjectionV1Error,
            "unclassified fields: future_team_schedule",
        ):
            project_live_state_for_policy_v1(
                state,
                registry,
                _health_registry(
                    ((0, 0, 1000.0, 1000.0), (1, 500, 9000.0, 9000.0))
                ),
            )

    def test_current_target_raw_hp_and_armor_hypotheses_are_not_policy_visible(self):
        left_state = _state(
            [], config_digest="1" * 64, environment_generation=17
        )
        right_state = deepcopy(left_state)
        right_state.update(
            {
                "target_health": 1800.0,
                "target_health_max": 2000.0,
                "target_health_percent": 90.0,
                "target_armor": 9876.0,
                "effective_target_armor": 9654.0,
                "encounter_health_target": 2000.0,
                "dynamic_team_response": {"suffix_model": "right"},
                "wake_ready": {"future_wake": 999_999},
                "press_clock": {
                    "enabled": True,
                    "period_ms": 100,
                    "phase_ms": 0,
                    "ready": True,
                    "press_index": 7,
                },
            }
        )
        left_state["dynamic_team_response"] = {"suffix_model": "left"}
        left_state["wake_ready"] = {"future_wake": 123_456}
        left_state["press_clock"] = {
            "enabled": True,
            "period_ms": 100,
            "phase_ms": 0,
            "ready": True,
            "press_index": 3,
        }
        left_state["dynamic_team_background"][
            "responsive_damage_applications_processed"
        ] = 3
        right_state["dynamic_team_background"][
            "responsive_damage_applications_processed"
        ] = 91
        left_life = left_state["dynamic_team_background"]["targets"][0]
        right_life = right_state["dynamic_team_background"]["targets"][0]
        left_semantics = left_state["dynamic_target_semantics"]["targets"][0]
        right_semantics = right_state["dynamic_target_semantics"]["targets"][0]
        left_semantics["maximum_health"] = 1000.0
        right_life["initial_health"] = 2000.0
        right_life["current_health"] = 1800.0
        right_semantics["maximum_health"] = 2000.0
        right_semantics["current_health"] = 1800.0
        right_semantics["effective_armor"] = 9999.0
        registry = TargetIntroductionRegistryV1(
            targets=(TargetIntroductionV1(0, 0),)
        )
        health = _health_registry(((0, 0, 1000.0, 1000.0),))

        left = project_cat2_policy_input_v1(
            _policy_input(left_state, run_digest="3" * 64), registry, health
        )
        right = project_cat2_policy_input_v1(
            _policy_input(right_state, run_digest="4" * 64), registry, health
        )

        self.assertEqual(
            canonical_policy_observation_bytes_v1(left.policy_input),
            canonical_policy_observation_bytes_v1(right.policy_input),
        )
        projected = left.policy_input["live_state"]
        self.assertEqual(800.0, projected["target_health"])
        self.assertEqual(1000.0, projected["target_health_max"])
        self.assertEqual(
            1000.0,
            projected["dynamic_target_semantics"]["targets"][0][
                "maximum_health"
            ],
        )
        self.assertNotIn("dynamic_team_response", projected)
        self.assertNotIn("wake_ready", projected)
        self.assertNotIn("press_clock", projected)
        self.assertNotIn("target_armor", projected)
        self.assertNotIn("effective_target_armor", projected)
        self.assertNotIn(
            "effective_armor",
            projected["dynamic_target_semantics"]["targets"][0],
        )
        self.assertNotIn(
            "responsive_damage_applications_processed",
            projected["dynamic_team_background"],
        )
        self.assertEqual(
            Cat2NewFuryParametricPolicyV1().decide(left.policy_input).to_wire(),
            Cat2NewFuryParametricPolicyV1().decide(right.policy_input).to_wire(),
        )

    def test_missing_or_future_health_contract_fails_closed(self):
        state = _state([], config_digest="2" * 64, environment_generation=18)
        registry = TargetIntroductionRegistryV1(
            targets=(TargetIntroductionV1(0, 0),)
        )
        with self.assertRaisesRegex(
            PolicyObservationCausalProjectionV1Error,
            "explicit TargetHealthPrefixRegistryV1",
        ):
            project_live_state_for_policy_v1(state, registry, None)
        with self.assertRaisesRegex(
            PolicyObservationCausalProjectionV1Error,
            "lacks a current-prefix health baseline",
        ):
            project_live_state_for_policy_v1(
                state,
                registry,
                _health_registry(((0, 101, 1000.0, 1000.0),)),
            )

    def test_zero_length_prefix_has_no_fabricated_team_rate(self):
        state = _state([], config_digest="6" * 64, environment_generation=19)
        state["time_ms"] = 0
        state["dynamic_team_background"]["targets"][0][
            "simulated_damage_applied"
        ] = 0.0
        state["dynamic_team_background"]["simulated_damage_applied"] = 0.0
        state["dynamic_team_background"]["combined_damage_applied"] = 0.0
        state["dynamic_team_background"]["targets"][0]["current_health"] = 1000.0
        state["dynamic_target_semantics"]["targets"][0]["current_health"] = 1000.0

        projected = project_live_state_for_policy_v1(
            state,
            TargetIntroductionRegistryV1(
                targets=(TargetIntroductionV1(0, 0),)
            ),
            _health_registry(((0, 0, 1000.0, 1000.0),)),
        ).state

        rate = projected["dynamic_team_background"]["prefix_damage_rate"]
        self.assertEqual(0, rate["elapsed_ms"])
        self.assertEqual(0.0, rate["combined_damage"])
        self.assertIsNone(rate["combined_damage_per_second"])
        self.assertNotIn("dynamic_team_response", projected)

    def test_dynamic_v4_state_uses_prefix_hp_and_drops_runtime_control(self):
        state = bound_state_v4(
            config_v4(), responsive_damage=10.0, wake_ready=True
        )
        projected = project_live_state_for_policy_v1(
            state,
            TargetIntroductionRegistryV1(
                targets=(TargetIntroductionV1(0, 0),)
            ),
            _health_registry(((0, 0, 120.0, 200.0),)),
        ).state

        self.assertEqual(110.0, projected["target_health"])
        self.assertEqual(200.0, projected["target_health_max"])
        self.assertEqual(55.0, projected["target_health_percent"])
        self.assertEqual(
            200.0,
            projected["dynamic_target_semantics"]["targets"][0][
                "maximum_health"
            ],
        )
        self.assertNotIn("dynamic_team_response", projected)
        self.assertNotIn("wake_ready", projected)


if __name__ == "__main__":
    unittest.main()
