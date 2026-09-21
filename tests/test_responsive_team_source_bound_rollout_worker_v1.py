from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

from o2o_dps import chronicle_external_teammate_response_hpc_v1 as hpc_v1
from o2o_dps import chronicle_external_teammate_response_model_v1 as response_v1
from o2o_dps.policy_observation_causal_projection_v1 import (
    TargetHealthPrefixBaselineV1,
    TargetHealthPrefixRegistryV1,
    TargetIntroductionRegistryV1,
    TargetIntroductionV1,
)
from o2o_dps.responsive_team_bridge_adapter_v1 import (
    EVENT_RECEIPT_SCHEMA,
    EVENT_RECEIPTS_COMMAND,
    EVENT_RECEIPTS_SCHEMA,
    LoadedResponsiveTeammateModelV1,
    TeammateModelProvenanceV1,
)
from o2o_dps.responsive_team_hpc_result_loader_v1 import (
    CurrentSourceDeclarationV1,
)
from o2o_dps.responsive_team_runtime_store_v1 import (
    STORE_SCHEMA,
    build_responsive_team_runtime_store_v1,
)
from o2o_dps.responsive_team_source_bound_rollout_worker_v1 import (
    DEVELOPMENT_WIRE_SMOKE_SCOPE,
    ResponsiveTeamBranchPlanV1,
    ResponsiveTeamRuntimeStoreBindingV1,
    ResponsiveTeamSourceBoundRolloutWorkerV1,
    ResponsiveTeamSourceBoundRolloutWorkerV1Error,
    responsive_team_source_prefix_content_sha256_v1,
    run_responsive_team_source_bound_rollout_worker_v1,
)
from o2o_dps.sim_bridge_dynamic_v4 import (
    DYNAMIC_TARGET_SEMANTICS_SCHEMA_V4,
    DynamicLoadReceiptV4,
    DynamicLoadResultV4,
    DynamicTargetHealthV4,
    DynamicTargetSemanticsConfigV4,
    SimulatorBridgeDynamicV4,
)
from o2o_dps.sim_bridge import BackgroundDamageEventV1
from o2o_dps.sim_bridge_dynamic_v2 import DynamicAttackabilityEventV2


TEAMMATE = "0x00000000000000AA"
CANDIDATE = "0x00000000000000CC"
TARGET = "0xF130000001000001"
RESULT_SHA = hashlib.sha256(b"responsive-result").hexdigest()
STAGE5_SHA = hashlib.sha256(b"stage5-source").hexdigest()
COMPONENT = "held-out-component"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SIMULATOR_ROOT = PROJECT_ROOT.parent / "wowsims-turtle"
V26_BRIDGE = (
    PROJECT_ROOT
    / "bin"
    / "o2obridge.seedfix-v26.observed-damage-v2.withdb.goamd64v1.windows-amd64.exe"
)


@dataclass(frozen=True)
class _DamageBatch:
    environment_generation: int
    config_digest: str
    cursor: int
    next_cursor: int
    receipts: tuple[object, ...]


class _DynamicV4ContractBridge:
    """Small executable wire double; the test does not claim simulator evidence."""

    def __init__(self) -> None:
        self.generation = 1
        self.config_digest = ""
        self.maximum_health = 0.0
        self.health = 0.0
        self.now = 0
        self.armed: dict | None = None
        self.team_receipts: list[dict] = []
        self.damage_ordinal = 0
        self.load_count = 0

    def load_dynamic_v4(self, request, seed, config):
        del request, seed
        self.load_count += 1
        self.config_digest = config.content_sha256
        self.maximum_health = config.target_health[0].maximum_health
        self.health = config.target_health[0].current_health
        receipt = DynamicLoadReceiptV4(
            schema=DYNAMIC_TARGET_SEMANTICS_SCHEMA_V4,
            config_digest=config.content_sha256,
            environment_generation=self.generation,
            target_count=1,
            background_event_count=0,
            attackability_event_count=0,
            effective_armor_event_count=0,
            same_timestamp_order=config.same_timestamp_order,
            retarget_mode=config.retarget_mode,
            idle_advance_mode=config.idle_advance_mode,
            idle_advance_horizon_ms=config.idle_advance_horizon_ms,
            idle_advance_receipt_schema="o2o_dynamic_idle_advance_receipts/v3",
        )
        return DynamicLoadResultV4(receipt=receipt, state=self.state())

    def state(self):
        ready = None
        if self.armed is not None and self.now >= self.armed["time_ms"]:
            ready = {
                key: self.armed[key]
                for key in ("schema", "model_content_sha256", "wake_id", "time_ms")
            }
        dead = self.health <= 0
        return {
            "time_ms": self.now,
            "needs_input": False,
            "finished": dead,
            "wake_ready": ready,
            "dynamic_team_background": {
                "targets": [
                    {
                        "target_index": 0,
                        "initial_health": 5.0,
                        "current_health": self.health,
                        "simulated_damage_applied": 0.0,
                        "background_damage_applied": 0.0,
                        "dead": dead,
                    }
                ]
            },
            "dynamic_target_semantics": {
                "targets": [
                    {
                        "target_index": 0,
                        "maximum_health": self.maximum_health,
                        "current_health": self.health,
                        "dead": dead,
                        "attackable": not dead,
                    }
                ]
            },
        }

    def advance(self):
        if self.armed is None:
            raise AssertionError("test bridge has no pending wake")
        self.now = self.armed["time_ms"]
        return self.state()

    def dynamic_damage_receipts(self, *, cursor):
        return _DamageBatch(
            self.generation, self.config_digest, cursor, cursor, ()
        )

    def dynamic_candidate_damage_receipts(self, *, cursor):
        return _DamageBatch(
            self.generation, self.config_digest, cursor, cursor, ()
        )

    def _request(self, command, **payload):
        if command == "arm_dynamic_team_wake":
            self.armed = dict(payload["responsive"])
            return {
                "responsive_team_wake": {**self.armed, "status": "ARMED"},
                "environment_generation": self.generation,
            }
        if command == "emit_dynamic_team_event":
            event = payload["responsive"]
            if self.armed is None or event["wake_id"] != self.armed["wake_id"]:
                raise AssertionError("event is not bound to the armed wake")
            observed = float(event["observed_damage"])
            requested = float(event["requested_damage"])
            if observed != requested:
                raise AssertionError("targeted test event must preserve observed damage")
            applied = min(requested, self.health)
            self.health -= applied
            self.damage_ordinal += 1
            receipt = {
                "schema": EVENT_RECEIPT_SCHEMA,
                "model_content_sha256": event["model_content_sha256"],
                "wake_id": event["wake_id"],
                "event_id": event["event_id"],
                "time_ms": self.now,
                "actor_guid": event["actor_guid"],
                "event_type": event["event_type"],
                "target_index": event["target_index"],
                "observed_damage": observed,
                "requested_damage": requested,
                "applied_damage": applied,
                "overkill_damage": requested - applied,
                "killed": self.health <= 0,
                "status": "APPLIED",
                "damage_ordinal": self.damage_ordinal,
                "current_health": self.health,
                "all_targets_dead": self.health <= 0,
            }
            self.team_receipts.append(receipt)
            self.armed = None
            return {
                "responsive_team_event": receipt,
                "environment_generation": self.generation,
            }
        if command == EVENT_RECEIPTS_COMMAND:
            cursor = payload["cursor"]
            rows = self.team_receipts[cursor:]
            return {
                "dynamic_team_response_receipts": {
                    "schema": EVENT_RECEIPTS_SCHEMA,
                    "model_content_sha256": self.team_receipts[-1][
                        "model_content_sha256"
                    ],
                    "environment_generation": self.generation,
                    "cursor": cursor,
                    "next_cursor": cursor + len(rows),
                    "receipts": rows,
                },
                "environment_generation": self.generation,
            }
        raise AssertionError(command)


def _selected_model(
    variant: str = response_v1.ABLATION_D,
) -> LoadedResponsiveTeammateModelV1:
    model = response_v1.HierarchicalMarkedSemiMarkovV1(
        variant_id=variant,
        min_guid_events=1,
        min_class_spec_events=1,
        min_class_events=1,
    )
    token = json.dumps(
        ["DMG", 23881, "DIRECT_FRIENDLY_PLAYER"], separators=(",", ":")
    )
    context = ("GLOBAL",)
    model.delay_counts[("GLOBAL", "WAVE_START")][1] = 1
    model.delay_counts[("GLOBAL", "PREVIOUS_ACTOR_EVENT")][1] = 1
    model.mark_counts[context][token] = 2
    model.target_counts[(context, token)]["STAY_ALIVE"] = 2
    model.damage_counts[(context, token)][4] = 2
    model.target_damage_joint_counts[(context, token)][("STAY_ALIVE", 4)] = 2
    model.spell_name_counts[token]["Bloodthirst"] = 1
    model.row_count = 2
    serialized = hpc_v1.serialize_model_v1(model)
    model_sha = hpc_v1._canonical_sha256(serialized)
    model.model_content_sha256 = model_sha
    provenance = TeammateModelProvenanceV1(
        source_artifact_schema=hpc_v1.RESULT_SCHEMA,
        source_artifact_content_sha256=RESULT_SHA,
        model_content_sha256=model_sha,
        variant_id=variant,
        training_scope="SOURCE_BOUND_STAGE5_TRAIN_COMPONENTS_DEVELOPMENT_ONLY",
        current_source_held_out=True,
    )
    return LoadedResponsiveTeammateModelV1(
        model=model,
        provenance=provenance,
        result_content_sha256=RESULT_SHA,
        current_source_evidence={
            "result_source_stage5_content_sha256": STAGE5_SHA,
            "current_source_stage5_content_sha256": STAGE5_SHA,
            "current_source_component_id": COMPONENT,
            "validation_component_ids": [COMPONENT],
            "current_source_held_out": True,
        },
    )


def _runtime() -> response_v1.DynamicTeamRuntimeV1:
    runtime = response_v1.DynamicTeamRuntimeV1(
        actors=(
            {"player_guid": TEAMMATE, "class": "MAGE", "spec_key": "MAGE_FIRE"},
            {"player_guid": CANDIDATE, "class": "WARRIOR", "spec_key": "FURY"},
        ),
        target_health_by_guid={TARGET: 5.0},
    )
    runtime.actors[TEAMMATE].current_target_guid = TARGET
    return runtime


def _config(
    *, current_health: float = 5.0, horizon_ms: int = 1_000
) -> DynamicTargetSemanticsConfigV4:
    return DynamicTargetSemanticsConfigV4(
        target_health=(DynamicTargetHealthV4(0, 20.0, current_health),),
        idle_advance_horizon_ms=horizon_ms,
    )


def _registries():
    return (
        TargetIntroductionRegistryV1(targets=(TargetIntroductionV1(0, 0),)),
        TargetHealthPrefixRegistryV1(
            targets=(
                TargetHealthPrefixBaselineV1(
                    0, 0, 5.0, 20.0, 0.0, 0.0
                ),
            )
        ),
    )


def _current_source_evidence() -> dict:
    return {
        "schema": f"{STORE_SCHEMA}/current_source_evidence",
        "result_source_stage5_content_sha256": STAGE5_SHA,
        "current_source_stage5_content_sha256": STAGE5_SHA,
        "current_source_component_id": COMPONENT,
        "validation_component_ids": [COMPONENT],
        "same_stage5_source": True,
        "component_in_validation_split": True,
        "current_source_held_out": True,
        "evidence_status": "BOUND_SOURCE_VALIDATION_COMPONENT",
        "rollout_worker_loaded_reducer_json": False,
    }


def _prefix_digest(
    runtime, introductions, health, *, candidate_actor_guid: str = CANDIDATE
) -> str:
    return responsive_team_source_prefix_content_sha256_v1(
        runtime=runtime,
        candidate_actor_guid=candidate_actor_guid,
        target_guid_by_index=(TARGET,),
        target_introduction_registry=introductions,
        target_health_prefix_registry=health,
        current_source_evidence=_current_source_evidence(),
    )


def _real_request(maximum_health: float) -> dict:
    with (
        PROJECT_ROOT / "configs" / "wowsims" / "fury_warrior_clean_dual.json"
    ).open("r", encoding="utf-8") as stream:
        request = json.load(stream)
    request["encounter"]["duration"] = 2
    request["encounter"]["durationVariation"] = 0
    request["encounter"]["useHealth"] = True
    target = deepcopy(request["encounter"]["targets"][0])
    target["name"] = "Responsive worker v26 smoke target"
    target["swingSpeed"] = 0
    target["minBaseDamage"] = 0
    target["damageSpread"] = 0
    target["parryHaste"] = False
    while len(target["stats"]) <= 34:
        target["stats"].append(0)
    target["stats"][34] = maximum_health
    request["encounter"]["targets"] = [target]
    request["simOptions"]["iterations"] = 1
    request["simOptions"]["interactive"] = True
    return request


class ResponsiveTeamSourceBoundRolloutWorkerV1Tests(unittest.TestCase):
    def _worker_kwargs(
        self,
        directory: str,
        *,
        variant: str = response_v1.ABLATION_D,
        horizon_ms: int = 1_000,
    ):
        selected = _selected_model(variant)
        store_path = Path(directory) / "selected.sqlite3"
        build_responsive_team_runtime_store_v1(store_path, selected)
        config = _config(horizon_ms=horizon_ms)
        introductions, health = _registries()
        runtime = _runtime()
        return {
            "bridge": _DynamicV4ContractBridge(),
            "store": ResponsiveTeamRuntimeStoreBindingV1(
                path=store_path,
                expected_result_content_sha256=RESULT_SHA,
                expected_model_content_sha256=(
                    selected.provenance.model_content_sha256
                ),
                variant_id=variant,
                current_source=CurrentSourceDeclarationV1(
                    stage5_content_sha256=STAGE5_SHA,
                    component_id=COMPONENT,
                    declared_held_out=True,
                ),
            ),
            "runtime": runtime,
            "candidate_actor_guid": CANDIDATE,
            "target_guid_by_index": (TARGET,),
            "target_introduction_registry": introductions,
            "target_health_prefix_registry": health,
            "branch_plan": ResponsiveTeamBranchPlanV1(
                pair_id="pair-1",
                branch_id="branch-a",
                candidate_suffix_id="candidate-a",
                prefix_content_sha256=_prefix_digest(
                    runtime, introductions, health
                ),
                simulator_seed=7,
                teammate_seed=11,
                dynamic_config_sha256=config.content_sha256,
            ),
            "config": config,
        }

    def test_node_local_store_to_loaded_adapter_to_wake_drive_and_close(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            values = self._worker_kwargs(temporary)
            config = values.pop("config")

            def rollout(bridge):
                loaded = bridge.load_dynamic_v4({}, 7, config)
                self.assertIsNone(loaded.state["wake_ready"])
                terminal = bridge.advance()
                return {"finished": terminal["finished"], "health": bridge.health}

            result = run_responsive_team_source_bound_rollout_worker_v1(
                rollout, **values
            )
            self.assertEqual(
                "COMPLETE_DEVELOPMENT_RESPONSIVE_TEAM_WIRE_RUN", result["status"]
            )
            self.assertEqual({"finished": True, "health": 0.0}, result["rollout_artifact"])
            evidence = result["worker_evidence"]
            self.assertTrue(evidence["runtime_store_closed"])
            self.assertFalse(evidence["rollout_worker_loaded_reducer_json"])
            drive = evidence["drive"]
            self.assertTrue(drive["atomic_loaded_model_passed_to_adapter"])
            self.assertEqual(1, drive["responsive_event_count"])
            self.assertEqual(5.0, drive["responsive_applied_damage"])
            self.assertEqual({"APPLIED": 1}, drive["responsive_status_counts"])
            self.assertFalse(result["comparison_authorized"])
            self.assertFalse(result["training_authorized"])

    def test_worker_passes_exclusive_horizon_and_accepts_no_initial_wake(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            values = self._worker_kwargs(temporary, horizon_ms=1)
            config = values.pop("config")
            worker = ResponsiveTeamSourceBoundRolloutWorkerV1(**values)
            with worker as driven:
                loaded = driven.load_dynamic_v4({}, 7, config)
                self.assertIsNone(loaded.state["wake_ready"])
                self.assertIsNone(driven.initial_wake)
                self.assertIsNone(driven._bridge.armed)
                evidence = driven.evidence()
                self.assertEqual(1, evidence["wake_horizon_exclusive_ms"])
                discarded = evidence["discarded_deadlines_at_or_after_horizon"]
                self.assertEqual(1, len(discarded))
                self.assertGreaterEqual(discarded[0]["time_ms"], 1)
                self.assertFalse(discarded[0]["go_arm_dynamic_team_wake_called"])

    def test_context_closes_sqlite_when_rollout_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            values = self._worker_kwargs(temporary)
            values.pop("config")
            worker = ResponsiveTeamSourceBoundRolloutWorkerV1(**values)
            with self.assertRaisesRegex(RuntimeError, "rollout failed"):
                with worker:
                    model = worker.loaded_model.model
                    raise RuntimeError("rollout failed")
            self.assertTrue(worker.store_closed)
            with self.assertRaises(sqlite3.ProgrammingError):
                model.sample_delay(
                    actor={"player_guid": TEAMMATE, "class": "MAGE", "spec_key": "MAGE_FIRE"},
                    timing_state=_runtime().snapshot_for_actor(TEAMMATE),
                    rng=__import__("random").Random(1),
                )

    def test_comparison_or_training_scope_is_rejected_before_store_open(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            values = self._worker_kwargs(temporary)
            values.pop("config")
            values["store"] = ResponsiveTeamRuntimeStoreBindingV1(
                path=Path(temporary) / "does-not-exist.sqlite3",
                expected_result_content_sha256=RESULT_SHA,
                expected_model_content_sha256="b" * 64,
                variant_id=response_v1.ABLATION_D,
                current_source=values["store"].current_source,
            )
            with self.assertRaisesRegex(
                ResponsiveTeamSourceBoundRolloutWorkerV1Error,
                "no comparison or training authority",
            ):
                ResponsiveTeamSourceBoundRolloutWorkerV1(
                    **values, execution_scope="POLICY_VALUE_COMPARISON"
                )

    def test_max_current_hp_mismatch_fails_closed_and_closes_store(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            values = self._worker_kwargs(temporary)
            values.pop("config")
            mismatched = _config(current_health=4.0)
            values["branch_plan"] = ResponsiveTeamBranchPlanV1(
                pair_id="pair-1",
                branch_id="branch-a",
                candidate_suffix_id="candidate-a",
                prefix_content_sha256=_prefix_digest(
                    values["runtime"],
                    values["target_introduction_registry"],
                    values["target_health_prefix_registry"],
                ),
                simulator_seed=7,
                teammate_seed=11,
                dynamic_config_sha256=mismatched.content_sha256,
            )
            worker = ResponsiveTeamSourceBoundRolloutWorkerV1(**values)
            with self.assertRaisesRegex(
                ResponsiveTeamSourceBoundRolloutWorkerV1Error,
                "max/current HP differs",
            ):
                with worker as bridge:
                    bridge.load_dynamic_v4({}, 7, mismatched)
            self.assertTrue(worker.store_closed)

    def test_changed_runtime_or_registry_cannot_reuse_old_prefix_digest(self) -> None:
        for mutation in ("runtime", "registry", "candidate"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                values = self._worker_kwargs(temporary)
                config = values.pop("config")
                if mutation == "runtime":
                    values["runtime"].actors[TEAMMATE].current_target_guid = None
                else:
                    if mutation == "registry":
                        values["target_health_prefix_registry"] = (
                            TargetHealthPrefixRegistryV1(
                                targets=(
                                    TargetHealthPrefixBaselineV1(
                                        0, 0, 4.0, 20.0, 0.0, 0.0
                                    ),
                                )
                            )
                        )
                    else:
                        values["candidate_actor_guid"] = TEAMMATE
                worker = ResponsiveTeamSourceBoundRolloutWorkerV1(**values)
                with self.assertRaisesRegex(
                    ResponsiveTeamSourceBoundRolloutWorkerV1Error,
                    "prefix digest differs",
                ):
                    with worker as driven:
                        driven.load_dynamic_v4({}, 7, config)
                self.assertEqual(0, values["bridge"].load_count)
                self.assertTrue(worker.store_closed)

    def test_future_dynamic_suffix_does_not_change_source_prefix_identity(self) -> None:
        runtime = _runtime()
        introductions, health = _registries()
        base = _config()
        changed_future = DynamicTargetSemanticsConfigV4(
            target_health=(DynamicTargetHealthV4(0, 20.0, 5.0),),
            idle_advance_horizon_ms=1_000,
            attackability_events=(
                DynamicAttackabilityEventV2(0, 100, 0, False),
            ),
        )
        prefix = _prefix_digest(runtime, introductions, health)
        self.assertNotEqual(base.content_sha256, changed_future.content_sha256)
        left = ResponsiveTeamBranchPlanV1(
            "pair", "left", "candidate-left", prefix, 7, 11, base.content_sha256
        )
        right = ResponsiveTeamBranchPlanV1(
            "pair",
            "right",
            "candidate-right",
            prefix,
            7,
            11,
            changed_future.content_sha256,
        )
        self.assertEqual(left.prefix_content_sha256, right.prefix_content_sha256)
        self.assertNotEqual(left.dynamic_config_sha256, right.dynamic_config_sha256)

    def test_b_and_c_are_rejected_before_bridge_load(self) -> None:
        for variant in (response_v1.ABLATION_B, response_v1.ABLATION_C):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as temporary:
                values = self._worker_kwargs(temporary, variant=variant)
                values.pop("config")
                worker = ResponsiveTeamSourceBoundRolloutWorkerV1(**values)
                with self.assertRaisesRegex(
                    ResponsiveTeamSourceBoundRolloutWorkerV1Error,
                    "requires ablation D",
                ):
                    with worker:
                        pass
                self.assertEqual(0, values["bridge"].load_count)
                self.assertTrue(worker.store_closed)

    def test_fixed_background_is_rejected_before_bridge_load(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            values = self._worker_kwargs(temporary)
            values.pop("config")
            config = DynamicTargetSemanticsConfigV4(
                target_health=(DynamicTargetHealthV4(0, 20.0, 5.0),),
                idle_advance_horizon_ms=1_000,
                background_damage_events=(
                    BackgroundDamageEventV1(0, 100, 0, "fixed-team", 1.0),
                ),
            )
            values["branch_plan"] = ResponsiveTeamBranchPlanV1(
                pair_id="pair-1",
                branch_id="branch-a",
                candidate_suffix_id="candidate-a",
                prefix_content_sha256=_prefix_digest(
                    values["runtime"],
                    values["target_introduction_registry"],
                    values["target_health_prefix_registry"],
                ),
                simulator_seed=7,
                teammate_seed=11,
                dynamic_config_sha256=config.content_sha256,
            )
            worker = ResponsiveTeamSourceBoundRolloutWorkerV1(**values)
            with self.assertRaisesRegex(
                ResponsiveTeamSourceBoundRolloutWorkerV1Error,
                "rejects fixed background damage",
            ):
                with worker as driven:
                    driven.load_dynamic_v4({}, 7, config)
            self.assertEqual(0, values["bridge"].load_count)
            self.assertTrue(worker.store_closed)

    def test_future_target_arrival_is_not_accepted_as_checkpoint_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            values = self._worker_kwargs(temporary)
            config = values.pop("config")
            introductions = TargetIntroductionRegistryV1(
                targets=(TargetIntroductionV1(0, 100),)
            )
            health = TargetHealthPrefixRegistryV1(
                targets=(
                    TargetHealthPrefixBaselineV1(
                        0, 100, 5.0, 20.0, 0.0, 0.0
                    ),
                )
            )
            values["target_introduction_registry"] = introductions
            values["target_health_prefix_registry"] = health
            values["branch_plan"] = ResponsiveTeamBranchPlanV1(
                pair_id="pair-1",
                branch_id="branch-a",
                candidate_suffix_id="candidate-a",
                prefix_content_sha256=_prefix_digest(
                    values["runtime"], introductions, health
                ),
                simulator_seed=7,
                teammate_seed=11,
                dynamic_config_sha256=config.content_sha256,
            )
            worker = ResponsiveTeamSourceBoundRolloutWorkerV1(**values)
            with self.assertRaisesRegex(
                ResponsiveTeamSourceBoundRolloutWorkerV1Error,
                "target was not prefix-visible",
            ):
                with worker as driven:
                    driven.load_dynamic_v4({}, 7, config)
            self.assertEqual(1, values["bridge"].load_count)
            self.assertTrue(worker.store_closed)

    def test_return_without_dynamic_v4_load_is_not_silently_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            values = self._worker_kwargs(temporary)
            values.pop("config")
            with self.assertRaisesRegex(
                ResponsiveTeamSourceBoundRolloutWorkerV1Error,
                "without calling responsive load_dynamic_v4",
            ):
                run_responsive_team_source_bound_rollout_worker_v1(
                    lambda _bridge: {"not_loaded": True}, **values
                )

    @unittest.skipUnless(
        os.name == "nt" and V26_BRIDGE.is_file(),
        "responsive Windows v26 bridge is unavailable",
    )
    def test_real_v26_store_adapter_worker_emits_one_responsive_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            selected = _selected_model()
            store_path = Path(temporary) / "selected.sqlite3"
            build_responsive_team_runtime_store_v1(store_path, selected)
            maximum = 1_000_000.0
            current = 500_000.0
            config = DynamicTargetSemanticsConfigV4(
                target_health=(DynamicTargetHealthV4(0, maximum, current),),
                idle_advance_horizon_ms=2_000,
            )
            runtime = response_v1.DynamicTeamRuntimeV1(
                actors=(
                    {
                        "player_guid": TEAMMATE,
                        "class": "MAGE",
                        "spec_key": "MAGE_FIRE",
                    },
                    {
                        "player_guid": CANDIDATE,
                        "class": "WARRIOR",
                        "spec_key": "FURY",
                    },
                ),
                target_health_by_guid={TARGET: current},
            )
            runtime.actors[TEAMMATE].current_target_guid = TARGET
            bridge = SimulatorBridgeDynamicV4(V26_BRIDGE, cwd=SIMULATOR_ROOT)
            try:
                worker = ResponsiveTeamSourceBoundRolloutWorkerV1(
                    bridge=bridge,
                    store=ResponsiveTeamRuntimeStoreBindingV1(
                        path=store_path,
                        expected_result_content_sha256=RESULT_SHA,
                        expected_model_content_sha256=(
                            selected.provenance.model_content_sha256
                        ),
                        variant_id=response_v1.ABLATION_D,
                        current_source=CurrentSourceDeclarationV1(
                            stage5_content_sha256=STAGE5_SHA,
                            component_id=COMPONENT,
                            declared_held_out=True,
                        ),
                    ),
                    runtime=runtime,
                    candidate_actor_guid=CANDIDATE,
                    target_guid_by_index=(TARGET,),
                    target_introduction_registry=TargetIntroductionRegistryV1(
                        targets=(TargetIntroductionV1(0, 0),)
                    ),
                    target_health_prefix_registry=TargetHealthPrefixRegistryV1(
                        targets=(
                            TargetHealthPrefixBaselineV1(
                                0, 0, current, maximum, 0.0, 0.0
                            ),
                        )
                    ),
                    branch_plan=ResponsiveTeamBranchPlanV1(
                        pair_id="real-v26-worker-smoke",
                        branch_id="branch-a",
                        candidate_suffix_id="wait-for-first-team-event",
                        prefix_content_sha256=(
                            responsive_team_source_prefix_content_sha256_v1(
                                runtime=runtime,
                                candidate_actor_guid=CANDIDATE,
                                target_guid_by_index=(TARGET,),
                                target_introduction_registry=(
                                    TargetIntroductionRegistryV1(
                                        targets=(TargetIntroductionV1(0, 0),)
                                    )
                                ),
                                target_health_prefix_registry=(
                                    TargetHealthPrefixRegistryV1(
                                        targets=(
                                            TargetHealthPrefixBaselineV1(
                                                0,
                                                0,
                                                current,
                                                maximum,
                                                0.0,
                                                0.0,
                                            ),
                                        )
                                    )
                                ),
                                current_source_evidence=_current_source_evidence(),
                            )
                        ),
                        simulator_seed=2026091305,
                        teammate_seed=11,
                        dynamic_config_sha256=config.content_sha256,
                    ),
                )
                with worker as driven:
                    loaded = driven.load_dynamic_v4(
                        _real_request(maximum), 2026091305, config
                    )
                    self.assertEqual(current, loaded.state["target_health"])
                    wake_time = driven.initial_wake["time_ms"]
                    after_wait = driven.wait(wake_time)
                    self.assertEqual(0, after_wait["time_ms"])
                    self.assertIsNone(after_wait.get("wake_ready"))
                    after_advance = driven.advance()
                    self.assertIsNone(after_advance.get("wake_ready"))
                    self.assertEqual(1, driven.responsive_event_count)
                    self.assertGreater(driven.applied_damage, 0)
                self.assertTrue(worker.store_closed)
                self.assertTrue(
                    worker.evidence()["drive"][
                        "atomic_loaded_model_passed_to_adapter"
                    ]
                )
            finally:
                bridge.close()
                if bridge._process.stdout is not None:
                    bridge._process.stdout.close()
                if bridge._process.stderr is not None:
                    bridge._process.stderr.close()

    def test_public_scope_constant_is_the_only_accepted_scope(self) -> None:
        self.assertEqual(
            "DEVELOPMENT_ONLY_SOURCE_BOUND_WIRE_SMOKE",
            DEVELOPMENT_WIRE_SMOKE_SCOPE,
        )


if __name__ == "__main__":
    unittest.main()
