from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import struct
from types import SimpleNamespace
import unittest

from o2o_dps.chronicle_external_teammate_response_model_v1 import (
    ABLATION_C,
    DynamicTeamRuntimeV1,
)
from o2o_dps.responsive_team_bridge_adapter_v1 import (
    ARM_WAKE_COMMAND,
    EMIT_EVENT_COMMAND,
    EVENT_RECEIPT_SCHEMA,
    EVENT_RECEIPTS_COMMAND,
    EVENT_RECEIPTS_SCHEMA,
    EVENT_SCHEMA,
    GAP_PROOF_SCHEMA,
    LoadedResponsiveTeammateModelV1,
    WAKE_SCHEMA,
    CausalBranchBindingV1,
    ResponsiveTeamBridgeAdapterV1,
    ResponsiveTeamBridgeAdapterV1Error,
    SAME_TIMESTAMP_ORDER,
    TeammateModelProvenanceV1,
    probe_responsive_team_protocol_gap_v1,
    required_go_protocol_contract_v1,
    validate_matched_causal_pair_v1,
)
from o2o_dps.sim_bridge_dynamic_v3 import SimulatorBridgeDynamicV3
from o2o_dps.sim_bridge import BackgroundDamageEventV1, DynamicTargetHealthV1
from o2o_dps.sim_bridge_dynamic_v2 import DynamicAttackabilityEventV2
from o2o_dps.sim_bridge_dynamic_v3 import DynamicTargetSemanticsConfigV3
from o2o_dps.sim_bridge_dynamic_v4 import (
    DynamicTargetHealthV4,
    DynamicTargetSemanticsConfigV4,
    SimulatorBridgeDynamicV4,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SIMULATOR_ROOT = PROJECT_ROOT.parent / "wowsims-turtle"
OLD_WINDOWS_BRIDGE = (
    PROJECT_ROOT
    / "bin"
    / "o2obridge.seedfix-v12.mechanicsfix.withdb.goamd64v1.windows-amd64.exe"
)
RESPONSIVE_WINDOWS_BRIDGE = (
    PROJECT_ROOT
    / "bin"
    / "o2obridge.seedfix-v26.observed-damage-v2.withdb.goamd64v1.windows-amd64.exe"
)
RESPONSIVE_WINDOWS_BRIDGE_SHA256 = (
    "2226bf02063455b6c15d7314b1bb1ea588a00f21d96d20f37a7814a909bc0a9a"
)
ZERO_DMG_RESPONSIVE_WINDOWS_BRIDGE = (
    PROJECT_ROOT
    / "bin"
    / "o2obridge.seedfix-v26.observed-damage-v2.withdb.goamd64v1.windows-amd64.exe"
)
ZERO_DMG_RESPONSIVE_WINDOWS_BRIDGE_SHA256 = (
    "2226bf02063455b6c15d7314b1bb1ea588a00f21d96d20f37a7814a909bc0a9a"
)

TEAMMATE = "0x00000000000000AA"
TEAMMATE_B = "0x00000000000000AB"
CANDIDATE = "0x00000000000000CC"
TARGET_A = "0xF130000001000001"
TARGET_B = "0xF130000002000002"


@dataclass(frozen=True)
class _CandidateBatch:
    environment_generation: int
    config_digest: str
    cursor: int
    next_cursor: int
    receipts: tuple[object, ...]


class _FixedModel:
    def __init__(self) -> None:
        self.delay_actor_inputs: list[dict] = []
        self.emission_actor_inputs: list[dict] = []
        self.emission_inputs: list[dict] = []

    def sample_delay(self, *, actor, timing_state, rng):
        self.delay_actor_inputs.append(dict(actor))
        return {
            "delay_ms": 100,
            "delay_bucket": 1,
            "context_level": "GLOBAL",
            "context": ["GLOBAL"],
            "support": 10,
        }

    def sample_emission(self, *, actor, emission_state, rng):
        self.emission_actor_inputs.append(dict(actor))
        self.emission_inputs.append(json.loads(json.dumps(emission_state)))
        return {
            "event_type": "DMG",
            "spell_id": 1337,
            "spell_name": "model-damage",
            "attribution_kind": "DIRECT_FRIENDLY_PLAYER",
            "attributed_player_guid": actor["player_guid"],
            "exact_source_guid": actor["player_guid"],
            "target_mode": "STAY_ALIVE",
            "sampled_damage": 7,
            "damage_bucket": 3,
            "context_level": "GLOBAL",
            "context": ["GLOBAL"],
            "support": 10,
        }


class _RandomDelayModel(_FixedModel):
    def sample_delay(self, *, actor, timing_state, rng):
        self.delay_actor_inputs.append(dict(actor))
        delay = rng.randint(1, 1000)
        return {
            "delay_ms": delay,
            "delay_bucket": 1,
            "context_level": "GLOBAL",
            "context": ["GLOBAL"],
            "support": 10,
        }


class _ZeroDamageDmgModel(_FixedModel):
    def sample_emission(self, *, actor, emission_state, rng):
        sampled = super().sample_emission(
            actor=actor,
            emission_state=emission_state,
            rng=rng,
        )
        sampled.update(
            {
                "spell_id": 11722,
                "spell_name": "Curse of the Elements",
                "sampled_damage": 0,
                "damage_bucket": 0,
            }
        )
        return sampled


class _UntargetedDmgModel(_FixedModel):
    def __init__(self, *, target_mode: str, sampled_damage: float) -> None:
        super().__init__()
        self.target_mode = target_mode
        self.sampled_damage = sampled_damage

    def sample_emission(self, *, actor, emission_state, rng):
        sampled = super().sample_emission(
            actor=actor,
            emission_state=emission_state,
            rng=rng,
        )
        sampled.update(
            {
                "spell_id": 11722,
                "spell_name": "untargeted-model-mark",
                "target_mode": self.target_mode,
                "sampled_damage": self.sampled_damage,
                "damage_bucket": 0,
            }
        )
        return sampled


class _ContractBridge:
    """Executable protocol double; it is not simulator evidence."""

    def __init__(
        self,
        *,
        generation: int,
        config_digest: str,
        target_health: tuple[float, float] = (10.0, 20.0),
        attackable: tuple[bool, bool] = (True, True),
    ) -> None:
        self.generation = generation
        self.config_digest = config_digest
        self.health = list(target_health)
        self.attackable = list(attackable)
        self.now = 0
        self.armed: dict | None = None
        self.candidate_receipts: list[object] = []
        self.background_receipts: list[object] = []
        self.team_receipts: list[dict] = []
        self.damage_ordinal = 0

    def add_candidate_damage(self, *, time_ms: int, target_index: int, damage: float) -> None:
        applied = min(damage, self.health[target_index])
        self.health[target_index] -= applied
        if applied > 0:
            self.damage_ordinal += 1
        self.candidate_receipts.append(
            SimpleNamespace(
                damage_ordinal=self.damage_ordinal if applied > 0 else 0,
                time_ms=time_ms,
                target_index=target_index,
                applied_damage=applied,
                action=SimpleNamespace(spell_id=1),
            )
        )

    def add_background_damage(
        self, *, time_ms: int, target_index: int, damage: float, event_id: str
    ) -> None:
        applied = min(damage, self.health[target_index])
        self.health[target_index] -= applied
        if applied > 0:
            self.damage_ordinal += 1
        self.background_receipts.append(
            SimpleNamespace(
                damage_ordinal=self.damage_ordinal if applied > 0 else 0,
                event_id=event_id,
                time_ms=time_ms,
                target_index=target_index,
                applied_damage=applied,
                status="APPLIED" if applied > 0 else "CANCELED_TARGET_DEAD",
            )
        )

    def add_terminal_background_cancel(
        self, *, time_ms: int, target_index: int, event_id: str
    ) -> None:
        self.background_receipts.append(
            SimpleNamespace(
                damage_ordinal=None,
                event_id=event_id,
                time_ms=time_ms,
                target_index=target_index,
                applied_damage=0.0,
                status="CANCELED_TARGET_DEAD",
            )
        )

    def dynamic_damage_receipts(self, *, cursor: int):
        rows = tuple(self.background_receipts[cursor:])
        return _CandidateBatch(
            environment_generation=self.generation,
            config_digest=self.config_digest,
            cursor=cursor,
            next_cursor=cursor + len(rows),
            receipts=rows,
        )

    def dynamic_candidate_damage_receipts(self, *, cursor: int):
        rows = tuple(self.candidate_receipts[cursor:])
        return _CandidateBatch(
            environment_generation=self.generation,
            config_digest=self.config_digest,
            cursor=cursor,
            next_cursor=cursor + len(rows),
            receipts=rows,
        )

    def state(self):
        assert self.armed is not None
        self.now = self.armed["time_ms"]
        return {
            "time_ms": self.now,
            "wake_ready": dict(self.armed),
            "dynamic_team_background": {
                "targets": [
                    {
                        "target_index": index,
                        "current_health": current_health,
                        "dead": current_health <= 0,
                    }
                    for index, current_health in enumerate(self.health)
                ]
            },
            "dynamic_target_semantics": {
                "targets": [
                    {
                        "target_index": index,
                        "current_health": current_health,
                        "dead": current_health <= 0,
                        "attackable": self.attackable[index],
                    }
                    for index, current_health in enumerate(self.health)
                ]
            },
        }

    def _request(self, command: str, **payload):
        if command == ARM_WAKE_COMMAND:
            wake = payload["responsive"]
            assert set(wake) == {
                "schema",
                "model_content_sha256",
                "wake_id",
                "time_ms",
            }
            self.armed = dict(wake)
            return {
                "responsive_team_wake": {**wake, "status": "ARMED"},
                "environment_generation": self.generation,
            }
        if command == EMIT_EVENT_COMMAND:
            event = payload["responsive"]
            assert self.armed is not None
            assert event["schema"] == EVENT_SCHEMA
            assert event["wake_id"] == self.armed["wake_id"]
            assert self.now == self.armed["time_ms"]
            target_index = event["target_index"]
            observed = float(event["observed_damage"])
            requested = float(event["requested_damage"])
            if target_index is None:
                assert requested == 0
                applied = 0.0
                current_health = None
                status = (
                    "OBSERVED_NON_HOSTILE_DAMAGE"
                    if event["event_type"] == "DMG" and observed > 0
                    else "OBSERVED_NO_DAMAGE"
                )
                ordinal = 0
            else:
                assert self.health[target_index] > 0
                if event["event_type"] == "DMG":
                    assert observed == requested
                canceled_unattackable = (
                    event["event_type"] == "DMG"
                    and not self.attackable[target_index]
                )
                applied = (
                    0.0
                    if canceled_unattackable
                    else (
                        min(requested, self.health[target_index])
                        if event["event_type"] == "DMG"
                        else 0.0
                    )
                )
                self.health[target_index] -= applied
                current_health = self.health[target_index]
                status = (
                    "CANCELED_TARGET_UNATTACKABLE"
                    if canceled_unattackable
                    else ("APPLIED" if applied > 0 else "OBSERVED_NO_DAMAGE")
                )
                ordinal = 0
                if event["event_type"] == "DMG":
                    self.damage_ordinal += 1
                    ordinal = self.damage_ordinal
            receipt = {
                "schema": EVENT_RECEIPT_SCHEMA,
                "model_content_sha256": event["model_content_sha256"],
                "wake_id": event["wake_id"],
                "event_id": event["event_id"],
                "time_ms": self.now,
                "actor_guid": event["actor_guid"],
                "event_type": event["event_type"],
                "target_index": target_index,
                "observed_damage": observed,
                "requested_damage": requested,
                "applied_damage": applied,
                "overkill_damage": requested - applied,
                "killed": target_index is not None and self.health[target_index] <= 0,
                "status": status,
                "damage_ordinal": ordinal,
                "current_health": current_health,
                "all_targets_dead": all(value <= 0 for value in self.health),
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


def _provenance() -> TeammateModelProvenanceV1:
    return TeammateModelProvenanceV1(
        source_artifact_schema="chronicle_external_teammate_response_hpc/v1/development_validation",
        source_artifact_content_sha256="a" * 64,
        model_content_sha256="e" * 64,
        variant_id=ABLATION_C,
        training_scope="OLD50_OVERLAP_DEVELOPMENT_TRAINING",
        current_source_held_out=False,
    )


def _loaded_model(
    model: object, provenance: TeammateModelProvenanceV1
) -> LoadedResponsiveTeammateModelV1:
    model.variant_id = provenance.variant_id
    model.model_content_sha256 = provenance.model_content_sha256
    return LoadedResponsiveTeammateModelV1(
        model=model,
        provenance=provenance,
        result_content_sha256=provenance.source_artifact_content_sha256,
        current_source_evidence={"schema": "fixture-current-source-evidence/v1"},
    )


def _binding(
    provenance: TeammateModelProvenanceV1, *, branch_id: str, suffix_id: str
) -> CausalBranchBindingV1:
    return CausalBranchBindingV1(
        pair_id="pair-1",
        branch_id=branch_id,
        candidate_suffix_id=suffix_id,
        prefix_content_sha256="c" * 64,
        simulator_seed=123,
        teammate_seed=456,
        environment_generation=1,
        dynamic_config_sha256="b" * 64,
        model_provenance_sha256=provenance.content_sha256,
    )


def _runtime(*, target_health: tuple[float, float] = (10, 20)) -> DynamicTeamRuntimeV1:
    runtime = DynamicTeamRuntimeV1(
        actors=(
            {
                "player_guid": TEAMMATE,
                "class": "MAGE",
                "spec_key": "MAGE_SPEC_NOT_AVAILABLE",
                "candidate_suffix_id": "metadata-leak-sentinel",
            },
            {
                "player_guid": CANDIDATE,
                "class": "WARRIOR",
                "spec_key": "WARRIOR_FURY",
            },
        ),
        target_health_by_guid={
            TARGET_A: target_health[0],
            TARGET_B: target_health[1],
        },
    )
    runtime.actors[TEAMMATE].current_target_guid = TARGET_A
    return runtime


def _multi_actor_runtime(
    *, target_health: tuple[float, float] = (10, 20)
) -> DynamicTeamRuntimeV1:
    runtime = DynamicTeamRuntimeV1(
        actors=(
            {
                "player_guid": TEAMMATE,
                "class": "MAGE",
                "spec_key": "MAGE_SPEC_NOT_AVAILABLE",
                "candidate_suffix_id": "metadata-leak-sentinel",
            },
            {
                "player_guid": TEAMMATE_B,
                "class": "ROGUE",
                "spec_key": "ROGUE_COMBAT",
            },
            {
                "player_guid": CANDIDATE,
                "class": "WARRIOR",
                "spec_key": "WARRIOR_FURY",
            },
        ),
        target_health_by_guid={
            TARGET_A: target_health[0],
            TARGET_B: target_health[1],
        },
    )
    runtime.actors[TEAMMATE].current_target_guid = TARGET_A
    runtime.actors[TEAMMATE_B].current_target_guid = TARGET_A
    return runtime


def _single_target_runtime(*, target_health: float) -> DynamicTeamRuntimeV1:
    runtime = DynamicTeamRuntimeV1(
        actors=(
            {
                "player_guid": TEAMMATE,
                "class": "MAGE",
                "spec_key": "MAGE_SPEC_NOT_AVAILABLE",
            },
            {
                "player_guid": CANDIDATE,
                "class": "WARRIOR",
                "spec_key": "WARRIOR_FURY",
            },
        ),
        target_health_by_guid={TARGET_A: target_health},
    )
    runtime.actors[TEAMMATE].current_target_guid = TARGET_A
    return runtime


def _two_target_request() -> dict:
    with (
        PROJECT_ROOT / "configs" / "wowsims" / "fury_warrior_clean_dual.json"
    ).open("r", encoding="utf-8") as stream:
        request = json.load(stream)
    request["encounter"]["duration"] = 2
    request["encounter"]["durationVariation"] = 0
    request["encounter"]["useHealth"] = True
    template = request["encounter"]["targets"][0]
    targets = []
    for index in range(2):
        target = deepcopy(template)
        target["name"] = f"Responsive teammate target {index}"
        target["swingSpeed"] = 0
        target["minBaseDamage"] = 0
        target["damageSpread"] = 0
        target["parryHaste"] = False
        while len(target["stats"]) <= 34:
            target["stats"].append(0)
        target["stats"][26] = 3000.0
        target["stats"][34] = 100.0
        targets.append(target)
    request["encounter"]["targets"] = targets
    request["simOptions"]["iterations"] = 1
    request["simOptions"]["interactive"] = True
    return request


def _dynamic_v4_wire(
    target_health: tuple[tuple[float, float], ...], *, horizon_ms: int
) -> dict:
    schema = "o2o_dynamic_target_semantics/v4"
    same_timestamp_order = SAME_TIMESTAMP_ORDER
    idle_mode = "CENTRAL_TO_NEXT_ATTACKABLE_OR_HORIZON"
    digest_document = {
        "attackability_events": [],
        "background_damage_events": [],
        "effective_armor_events": [],
        "idle_advance_horizon_ms": horizon_ms,
        "idle_advance_mode": idle_mode,
        "retarget_mode": "NEXT_ALIVE_CYCLIC",
        "same_timestamp_order": same_timestamp_order,
        "schema": schema,
        "target_health": [
            {
                "current_health_ieee754": struct.pack(">d", current).hex(),
                "maximum_health_ieee754": struct.pack(">d", maximum).hex(),
                "target_index": index,
            }
            for index, (maximum, current) in enumerate(target_health)
        ],
    }
    digest = hashlib.sha256(
        json.dumps(
            digest_document,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema": schema,
        "content_sha256": digest,
        "target_health": [
            {
                "target_index": index,
                "maximum_health": maximum,
                "current_health": current,
            }
            for index, (maximum, current) in enumerate(target_health)
        ],
        "background_damage_events": [],
        "attackability_events": [],
        "effective_armor_events": [],
        "idle_advance_mode": idle_mode,
        "idle_advance_horizon_ms": horizon_ms,
        "same_timestamp_order": same_timestamp_order,
        "retarget_mode": "NEXT_ALIVE_CYCLIC",
    }


class ResponsiveTeamBridgeAdapterV1Tests(unittest.TestCase):
    def test_direct_white6603_uses_own_head_and_aoe_does_not(self) -> None:
        class _WhiteModel(_FixedModel):
            def sample_emission(self, *, actor, emission_state, rng):
                sampled = super().sample_emission(
                    actor=actor, emission_state=emission_state, rng=rng
                )
                sampled.update(
                    spell_id=6603, spell_name="Attack", target_mode="SWITCH_ALIVE"
                )
                return sampled

            def sample_target_choice(self, **kwargs):
                assert kwargs["intent_kind"] == "WHITE6603"
                assert set(kwargs["eligible_target_guids"]) == {TARGET_A, TARGET_B}
                return {
                    "target_guid": TARGET_B,
                    "context_level": "GLOBAL",
                    "phase": "WHITE6603_RETARGET",
                    "support": 8,
                }

        provenance = _provenance()
        adapter = ResponsiveTeamBridgeAdapterV1(
            bridge=_ContractBridge(generation=1, config_digest="b" * 64),
            runtime=_runtime(), loaded_model=_loaded_model(_WhiteModel(), provenance),
            candidate_actor_guid=CANDIDATE,
            target_guid_by_index=(TARGET_A, TARGET_B),
            branch=_binding(provenance, branch_id="white6603-head", suffix_id="same-suffix"),
        )
        adapter.arm_next(TEAMMATE)
        result = adapter.emit_ready(TEAMMATE)
        self.assertEqual(result["local_runtime_transition"]["target_guid"], TARGET_B)
        self.assertEqual(result["target_choice_head"]["phase"], "WHITE6603_RETARGET")
        self.assertEqual(
            result["target_selection_basis"],
            "LEARNED_DIRECT_WHITE6603_PREFIX_TARGET_CHOICE",
        )
        self.assertEqual(adapter.runtime.actors[TEAMMATE].current_target_guid, TARGET_B)

        class _AoeModel(_WhiteModel):
            def sample_emission(self, *, actor, emission_state, rng):
                sampled = super().sample_emission(
                    actor=actor, emission_state=emission_state, rng=rng
                )
                sampled["spell_id"] = 1680
                return sampled

            def sample_target_choice(self, **kwargs):
                raise AssertionError("AoE result must not invoke the intent head")

        aoe = ResponsiveTeamBridgeAdapterV1(
            bridge=_ContractBridge(generation=1, config_digest="b" * 64),
            runtime=_runtime(), loaded_model=_loaded_model(_AoeModel(), provenance),
            candidate_actor_guid=CANDIDATE,
            target_guid_by_index=(TARGET_A, TARGET_B),
            branch=_binding(provenance, branch_id="aoe-no-head", suffix_id="same-suffix"),
        )
        aoe.arm_next(TEAMMATE)
        aoe_result = aoe.emit_ready(TEAMMATE)
        self.assertIsNone(aoe_result["target_choice_head"])
        self.assertEqual(
            aoe_result["target_selection_basis"],
            "ATTACKABLE_ALIVE_REGISTRY_UNCALIBRATED_DAMAGE_TARGET",
        )
        self.assertEqual(aoe.runtime.actors[TEAMMATE].current_target_guid, TARGET_A)

    def test_white6603_stay_uses_focus_without_redraw(self) -> None:
        class _StayWhiteModel(_FixedModel):
            def sample_emission(self, *, actor, emission_state, rng):
                sampled = super().sample_emission(
                    actor=actor, emission_state=emission_state, rng=rng
                )
                sampled["spell_id"] = 6603
                return sampled

            def sample_target_choice(self, **kwargs):
                raise AssertionError("live STAY focus is deterministic")

        provenance = _provenance()
        adapter = ResponsiveTeamBridgeAdapterV1(
            bridge=_ContractBridge(generation=1, config_digest="b" * 64),
            runtime=_runtime(), loaded_model=_loaded_model(_StayWhiteModel(), provenance),
            candidate_actor_guid=CANDIDATE,
            target_guid_by_index=(TARGET_A, TARGET_B),
            branch=_binding(provenance, branch_id="white6603-stay", suffix_id="same-suffix"),
        )
        adapter.arm_next(TEAMMATE)
        result = adapter.emit_ready(TEAMMATE)
        self.assertEqual(result["local_runtime_transition"]["target_guid"], TARGET_A)
        self.assertEqual(result["target_selection_basis"], "CURRENT_FOCUS_DIRECT_WHITE6603")

    def test_white6603_head_only_receives_attackable_registry(self) -> None:
        class _RestrictedWhiteModel(_FixedModel):
            def sample_emission(self, *, actor, emission_state, rng):
                sampled = super().sample_emission(
                    actor=actor, emission_state=emission_state, rng=rng
                )
                sampled.update(spell_id=6603, target_mode="SWITCH_ALIVE")
                return sampled

            def sample_target_choice(self, **kwargs):
                assert kwargs["intent_kind"] == "WHITE6603"
                assert kwargs["eligible_target_guids"] == [TARGET_A]
                return None

        provenance = _provenance()
        adapter = ResponsiveTeamBridgeAdapterV1(
            bridge=_ContractBridge(
                generation=1, config_digest="b" * 64,
                attackable=(True, False),
            ),
            runtime=_runtime(),
            loaded_model=_loaded_model(_RestrictedWhiteModel(), provenance),
            candidate_actor_guid=CANDIDATE,
            target_guid_by_index=(TARGET_A, TARGET_B),
            branch=_binding(provenance, branch_id="white6603-eligible", suffix_id="same-suffix"),
        )
        adapter.arm_next(TEAMMATE)
        result = adapter.emit_ready(TEAMMATE)
        self.assertEqual(result["local_runtime_transition"]["target_guid"], TARGET_A)
        self.assertEqual(
            result["target_selection_basis"],
            "ATTACKABLE_ALIVE_REGISTRY_WHITE6603_NO_CHOICE_SUPPORT",
        )

    def test_direct_start_uses_learned_attackable_choice_and_sets_focus(self) -> None:
        class _DirectedStartModel(_FixedModel):
            def sample_emission(self, *, actor, emission_state, rng):
                sampled = super().sample_emission(
                    actor=actor, emission_state=emission_state, rng=rng
                )
                sampled.update(
                    event_type="START", spell_id=23881, spell_name="Bloodthirst",
                    target_mode="SWITCH_ALIVE", sampled_damage=0, damage_bucket=0,
                )
                return sampled

            def sample_target_choice(self, **kwargs):
                assert set(kwargs["eligible_target_guids"]) == {TARGET_A, TARGET_B}
                assert kwargs["intent_kind"] == "START"
                return {
                    "target_guid": TARGET_B,
                    "context_level": "GLOBAL",
                    "phase": "START_FIRST_ACQUISITION",
                    "support": 5,
                }

        provenance = _provenance()
        bridge = _ContractBridge(generation=1, config_digest="b" * 64)
        adapter = ResponsiveTeamBridgeAdapterV1(
            bridge=bridge, runtime=_runtime(),
            loaded_model=_loaded_model(_DirectedStartModel(), provenance),
            candidate_actor_guid=CANDIDATE,
            target_guid_by_index=(TARGET_A, TARGET_B),
            branch=_binding(provenance, branch_id="directed-start", suffix_id="same-suffix"),
        )
        adapter.arm_next(TEAMMATE)
        result = adapter.emit_ready(TEAMMATE)
        self.assertEqual(result["local_runtime_transition"]["target_guid"], TARGET_B)
        self.assertEqual(
            result["target_selection_basis"],
            "LEARNED_DIRECT_START_PREFIX_TARGET_CHOICE",
        )
        self.assertEqual(adapter.runtime.actors[TEAMMATE].current_target_guid, TARGET_B)

    def test_adapter_rejects_model_identity_mutated_after_atomic_load(self) -> None:
        provenance = _provenance()
        model = _FixedModel()
        loaded = _loaded_model(model, provenance)
        model.variant_id = "D_NO_GUID_HISTORY"
        with self.assertRaisesRegex(
            ResponsiveTeamBridgeAdapterV1Error, "variant changed"
        ):
            ResponsiveTeamBridgeAdapterV1(
                bridge=_ContractBridge(generation=1, config_digest="b" * 64),
                runtime=_runtime(),
                loaded_model=loaded,
                candidate_actor_guid=CANDIDATE,
                target_guid_by_index=(TARGET_A, TARGET_B),
                branch=_binding(
                    provenance, branch_id="mismatch", suffix_id="same-suffix"
                ),
            )

        model.variant_id = provenance.variant_id
        model.model_content_sha256 = "0" * 64
        with self.assertRaisesRegex(
            ResponsiveTeamBridgeAdapterV1Error, "content identity changed"
        ):
            ResponsiveTeamBridgeAdapterV1(
                bridge=_ContractBridge(generation=1, config_digest="b" * 64),
                runtime=_runtime(),
                loaded_model=loaded,
                candidate_actor_guid=CANDIDATE,
                target_guid_by_index=(TARGET_A, TARGET_B),
                branch=_binding(
                    provenance, branch_id="mismatch", suffix_id="same-suffix"
                ),
            )

    def test_required_go_contract_names_two_stage_commands(self) -> None:
        contract = required_go_protocol_contract_v1()
        self.assertEqual(
            [ARM_WAKE_COMMAND, EMIT_EVENT_COMMAND, EVENT_RECEIPTS_COMMAND],
            contract["required_commands"],
        )
        self.assertTrue(contract["wake_contract"]["pause_at_exact_deadline_before_candidate"])
        self.assertTrue(
            contract["wake_contract"]["allows_rearm_while_policy_input_is_suspended"]
        )
        self.assertEqual(
            "TARGET_SEMANTICS_BEFORE_FIXED_BACKGROUND_BEFORE_RESPONSIVE_TEAM_WAKE_"
            "BEFORE_ENVIRONMENT_WAKE_BEFORE_CANDIDATE",
            contract["same_timestamp_order"],
        )
        self.assertTrue(
            contract["emit_contract"]["go_applies_damage_through_dynamic_target_lifecycle"]
        )
        self.assertTrue(
            contract["emit_contract"][
                "targeted_zero_damage_dmg_mark_preserved_with_positive_damage_ordinal"
            ]
        )
        self.assertTrue(
            contract["emit_contract"][
                "untargeted_zero_damage_dmg_mark_preserved_with_zero_damage_ordinal"
            ]
        )
        self.assertTrue(
            contract["emit_contract"][
                "untargeted_zero_damage_dmg_mark_enters_causal_prefix"
            ]
        )
        self.assertTrue(
            contract["emit_contract"][
                "wire_separates_observed_damage_from_hostile_requested_damage"
            ]
        )
        self.assertTrue(
            contract["emit_contract"][
                "positive_non_hostile_damage_preserved_without_hostile_hp_change"
            ]
        )
        self.assertTrue(
            contract["emit_contract"][
                "diagnostic_target_modes_never_resolve_to_live_targets"
            ]
        )
        self.assertNotIn(
            "zero_damage_dmg_mark_preserved_with_damage_ordinal",
            contract["emit_contract"],
        )
        self.assertFalse(
            contract["scientific_boundary"]["candidate_policy_identity_visible_to_model"]
        )

    def test_same_prefix_different_suffix_changes_live_teammate_target(self) -> None:
        provenance = _provenance()
        fast_binding = _binding(
            provenance, branch_id="fast", suffix_id="candidate-kills-a"
        )
        slow_binding = _binding(
            provenance, branch_id="slow", suffix_id="candidate-does-not-kill-a"
        )
        pair = validate_matched_causal_pair_v1(fast_binding, slow_binding)
        self.assertEqual("MATCHED_PREFIX_DISTINCT_CANDIDATE_SUFFIX", pair["status"])

        fast_bridge = _ContractBridge(generation=1, config_digest="b" * 64)
        slow_bridge = _ContractBridge(generation=1, config_digest="b" * 64)
        fast_model = _FixedModel()
        slow_model = _FixedModel()
        fast = ResponsiveTeamBridgeAdapterV1(
            bridge=fast_bridge,
            runtime=_runtime(),
            loaded_model=_loaded_model(fast_model, provenance),
            candidate_actor_guid=CANDIDATE,
            target_guid_by_index=(TARGET_A, TARGET_B),
            branch=fast_binding,
        )
        slow = ResponsiveTeamBridgeAdapterV1(
            bridge=slow_bridge,
            runtime=_runtime(),
            loaded_model=_loaded_model(slow_model, provenance),
            candidate_actor_guid=CANDIDATE,
            target_guid_by_index=(TARGET_A, TARGET_B),
            branch=slow_binding,
        )
        self.assertEqual(100, fast.arm_next(TEAMMATE)["time_ms"])
        self.assertEqual(100, slow.arm_next(TEAMMATE)["time_ms"])

        # This is the only branch difference after the common prefix.  The
        # authoritative candidate receipt is synchronized before the due wake.
        fast_bridge.add_candidate_damage(time_ms=50, target_index=0, damage=10)
        fast_result = fast.emit_ready(TEAMMATE)
        slow_result = slow.emit_ready(TEAMMATE)

        self.assertEqual(1, fast_result["wire_event"]["target_index"])
        self.assertEqual(0, slow_result["wire_event"]["target_index"])
        self.assertEqual(7.0, fast_result["wire_receipt"]["applied_damage"])
        self.assertEqual(7.0, slow_result["wire_receipt"]["applied_damage"])
        self.assertEqual(
            fast_result["wire_receipt"], fast_result["cursor_stream_receipt"]
        )
        self.assertEqual(
            slow_result["wire_receipt"], slow_result["cursor_stream_receipt"]
        )
        self.assertEqual(
            [TARGET_B],
            fast_model.emission_inputs[0]["target_state"]["alive_target_guids"],
        )
        self.assertEqual(
            [TARGET_A, TARGET_B],
            slow_model.emission_inputs[0]["target_state"]["alive_target_guids"],
        )
        serialized_inputs = json.dumps(
            fast_model.delay_actor_inputs
            + fast_model.emission_actor_inputs
            + fast_model.emission_inputs
            + slow_model.delay_actor_inputs
            + slow_model.emission_actor_inputs
            + slow_model.emission_inputs,
            sort_keys=True,
        )
        self.assertNotIn("candidate-kills-a", serialized_inputs)
        self.assertNotIn("candidate-does-not-kill-a", serialized_inputs)
        self.assertNotIn("metadata-leak-sentinel", serialized_inputs)
        self.assertEqual(
            {"player_guid", "class", "spec_key"},
            set(fast_result["model_actor_identity"]),
        )
        self.assertFalse(fast_result["comparison_eligible"])
        self.assertFalse(fast_result["deployment_eligible"])

    def test_fixed_background_damage_enters_prefix_before_live_retarget(self) -> None:
        provenance = _provenance()
        bridge = _ContractBridge(generation=1, config_digest="b" * 64)
        model = _FixedModel()
        adapter = ResponsiveTeamBridgeAdapterV1(
            bridge=bridge,
            runtime=_runtime(),
            loaded_model=_loaded_model(model, provenance),
            candidate_actor_guid=CANDIDATE,
            target_guid_by_index=(TARGET_A, TARGET_B),
            branch=_binding(
                provenance, branch_id="fixed-background", suffix_id="same-suffix"
            ),
        )
        adapter.arm_next(TEAMMATE)
        bridge.add_background_damage(
            time_ms=50,
            target_index=0,
            damage=10,
            event_id="fixed-kills-a",
        )
        result = adapter.emit_ready(TEAMMATE)

        self.assertEqual(1, adapter.background_cursor)
        self.assertEqual(0, adapter.candidate_cursor)
        self.assertEqual(1, result["wire_event"]["target_index"])
        self.assertEqual(
            "FIXED_BACKGROUND",
            result["authoritative_prefix_damage_receipts"][0]["source_kind"],
        )
        other = result["model_input_live_prefix"]["marked_activity"][
            "other_team_including_unattributed"
        ]
        self.assertEqual(1, other["damage_event_count_3000ms"])
        self.assertEqual(10, other["damage_amount_3000ms"])

    def test_terminal_fixed_background_cancel_has_no_prefix_damage(self) -> None:
        provenance = _provenance()
        bridge = _ContractBridge(generation=1, config_digest="b" * 64)
        adapter = ResponsiveTeamBridgeAdapterV1(
            bridge=bridge,
            runtime=_runtime(),
            loaded_model=_loaded_model(_FixedModel(), provenance),
            candidate_actor_guid=CANDIDATE,
            target_guid_by_index=(TARGET_A, TARGET_B),
            branch=_binding(
                provenance, branch_id="terminal-cancel", suffix_id="same-suffix"
            ),
        )
        bridge.add_background_damage(
            time_ms=10,
            target_index=0,
            damage=10,
            event_id="fixed-kills-a",
        )
        bridge.add_terminal_background_cancel(
            time_ms=50,
            target_index=0,
            event_id="post-death-cancel",
        )

        rows = adapter.sync_authoritative_damage_prefix()

        self.assertEqual(2, adapter.background_cursor)
        self.assertEqual(0.0, adapter.runtime.health[TARGET_A])
        self.assertEqual((1, None), tuple(row["damage_ordinal"] for row in rows))
        self.assertTrue(rows[0]["mirrored_into_model_prefix"])
        self.assertFalse(rows[1]["mirrored_into_model_prefix"])
        self.assertEqual("CANCELED_TARGET_DEAD", rows[1]["status"])

    def test_live_target_selection_excludes_alive_unattackable_target(self) -> None:
        provenance = _provenance()
        bridge = _ContractBridge(
            generation=1,
            config_digest="b" * 64,
            attackable=(False, True),
        )
        adapter = ResponsiveTeamBridgeAdapterV1(
            bridge=bridge,
            runtime=_runtime(),
            loaded_model=_loaded_model(_FixedModel(), provenance),
            candidate_actor_guid=CANDIDATE,
            target_guid_by_index=(TARGET_A, TARGET_B),
            branch=_binding(
                provenance, branch_id="attackable-filter", suffix_id="same-suffix"
            ),
        )
        adapter.arm_next(TEAMMATE)
        result = adapter.emit_ready(TEAMMATE)

        self.assertEqual([0, 1], result["bridge_alive_target_indices_before_emission"])
        self.assertEqual(
            [1],
            result[
                "bridge_attackable_alive_target_indices_before_emission"
            ],
        )
        self.assertEqual(1, result["wire_event"]["target_index"])
        self.assertEqual(TARGET_B, result["local_runtime_transition"]["target_guid"])

    def test_zero_damage_dmg_mark_is_observed_with_ordinal_without_hp_change(self) -> None:
        provenance = _provenance()
        bridge = _ContractBridge(generation=1, config_digest="b" * 64)
        adapter = ResponsiveTeamBridgeAdapterV1(
            bridge=bridge,
            runtime=_runtime(),
            loaded_model=_loaded_model(_ZeroDamageDmgModel(), provenance),
            candidate_actor_guid=CANDIDATE,
            target_guid_by_index=(TARGET_A, TARGET_B),
            branch=_binding(
                provenance,
                branch_id="zero-damage-dmg-mark",
                suffix_id="same-suffix",
            ),
        )

        adapter.arm_next(TEAMMATE)
        result = adapter.emit_ready(TEAMMATE)

        self.assertEqual("DMG", result["wire_event"]["event_type"])
        self.assertEqual(0.0, result["wire_event"]["observed_damage"])
        self.assertEqual(0.0, result["wire_event"]["requested_damage"])
        self.assertEqual("OBSERVED_NO_DAMAGE", result["wire_receipt"]["status"])
        self.assertEqual(1, result["wire_receipt"]["damage_ordinal"])
        self.assertEqual(10.0, result["wire_receipt"]["current_health"])
        self.assertEqual([10.0, 20.0], bridge.health)
        self.assertEqual({TARGET_A: 10.0, TARGET_B: 20.0}, adapter.runtime.health)
        self.assertTrue(
            result["local_runtime_transition"]["observed_in_model_prefix"]
        )
        self.assertEqual(
            '["DMG",11722,"DIRECT_FRIENDLY_PLAYER"]',
            adapter.runtime._replay.actors[TEAMMATE].last_mark_token,
        )

    def test_untargeted_zero_damage_dmg_mark_has_zero_ordinal_and_enters_prefix(
        self,
    ) -> None:
        for target_mode in ("NO_TARGET", "NON_HOSTILE_OR_UNKNOWN"):
            with self.subTest(target_mode=target_mode):
                provenance = _provenance()
                bridge = _ContractBridge(generation=1, config_digest="b" * 64)
                adapter = ResponsiveTeamBridgeAdapterV1(
                    bridge=bridge,
                    runtime=_runtime(),
                    loaded_model=_loaded_model(
                        _UntargetedDmgModel(
                            target_mode=target_mode,
                            sampled_damage=0,
                        ),
                        provenance,
                    ),
                    candidate_actor_guid=CANDIDATE,
                    target_guid_by_index=(TARGET_A, TARGET_B),
                    branch=_binding(
                        provenance,
                        branch_id=f"untargeted-zero-{target_mode}",
                        suffix_id="same-suffix",
                    ),
                )

                adapter.arm_next(TEAMMATE)
                result = adapter.emit_ready(TEAMMATE)

                self.assertIsNone(result["wire_event"]["target_index"])
                self.assertEqual(0.0, result["wire_event"]["observed_damage"])
                self.assertEqual(0.0, result["wire_event"]["requested_damage"])
                self.assertEqual(
                    "OBSERVED_NO_DAMAGE", result["wire_receipt"]["status"]
                )
                self.assertEqual(0, result["wire_receipt"]["damage_ordinal"])
                self.assertIsNone(result["wire_receipt"]["current_health"])
                self.assertEqual(
                    "MODEL_EXPLICIT_UNTARGETED_ZERO_DAMAGE",
                    result["target_selection_basis"],
                )
                self.assertIsNone(
                    result["local_runtime_transition"]["target_guid"]
                )
                self.assertTrue(
                    result["local_runtime_transition"][
                        "observed_in_model_prefix"
                    ]
                )
                self.assertEqual([10.0, 20.0], bridge.health)
                self.assertEqual(
                    {TARGET_A: 10.0, TARGET_B: 20.0}, adapter.runtime.health
                )
                self.assertEqual(
                    '["DMG",11722,"DIRECT_FRIENDLY_PLAYER"]',
                    adapter.runtime._replay.actors[TEAMMATE].last_mark_token,
                )
                self.assertEqual(
                    result["wire_receipt"], result["cursor_stream_receipt"]
                )

    def test_positive_non_hostile_damage_is_observed_without_hostile_hp_change(
        self,
    ) -> None:
        for target_mode in ("NO_TARGET", "NON_HOSTILE_OR_UNKNOWN"):
            with self.subTest(target_mode=target_mode):
                provenance = _provenance()
                bridge = _ContractBridge(generation=1, config_digest="b" * 64)
                adapter = ResponsiveTeamBridgeAdapterV1(
                    bridge=bridge,
                    runtime=_runtime(),
                    loaded_model=_loaded_model(
                        _UntargetedDmgModel(
                            target_mode=target_mode,
                            sampled_damage=7,
                        ),
                        provenance,
                    ),
                    candidate_actor_guid=CANDIDATE,
                    target_guid_by_index=(TARGET_A, TARGET_B),
                    branch=_binding(
                        provenance,
                        branch_id=f"positive-no-target-{target_mode}",
                        suffix_id="same-suffix",
                    ),
                )

                adapter.arm_next(TEAMMATE)
                result = adapter.emit_ready(TEAMMATE)

                self.assertIsNone(result["wire_event"]["target_index"])
                self.assertEqual(7.0, result["wire_event"]["observed_damage"])
                self.assertEqual(0.0, result["wire_event"]["requested_damage"])
                self.assertEqual(
                    "OBSERVED_NON_HOSTILE_DAMAGE",
                    result["wire_receipt"]["status"],
                )
                self.assertEqual(7.0, result["wire_receipt"]["observed_damage"])
                self.assertEqual(0.0, result["wire_receipt"]["requested_damage"])
                self.assertEqual(0.0, result["wire_receipt"]["applied_damage"])
                self.assertEqual(0.0, result["wire_receipt"]["overkill_damage"])
                self.assertEqual(0, result["wire_receipt"]["damage_ordinal"])
                self.assertIsNone(result["wire_receipt"]["current_health"])
                self.assertEqual(
                    "MODEL_EXPLICIT_OBSERVED_NON_HOSTILE_DAMAGE",
                    result["target_selection_basis"],
                )
                self.assertEqual([10.0, 20.0], bridge.health)
                self.assertEqual(
                    {TARGET_A: 10.0, TARGET_B: 20.0}, adapter.runtime.health
                )
                snapshot = adapter.runtime.snapshot_for_actor(TEAMMATE)
                self.assertEqual(
                    7,
                    snapshot["marked_activity"]["actor"][
                        "damage_amount_3000ms"
                    ],
                )
                self.assertEqual(
                    result["wire_receipt"], result["cursor_stream_receipt"]
                )

    def test_diagnostic_target_mode_fails_closed_before_live_target_selection(
        self,
    ) -> None:
        for target_mode in (
            "DEAD_TARGET_OBSERVED_DIAGNOSTIC",
            "UNSEEN_HOSTILE_CURRENT_LABEL",
        ):
            with self.subTest(target_mode=target_mode):
                provenance = _provenance()
                bridge = _ContractBridge(generation=1, config_digest="b" * 64)
                adapter = ResponsiveTeamBridgeAdapterV1(
                    bridge=bridge,
                    runtime=_runtime(),
                    loaded_model=_loaded_model(
                        _UntargetedDmgModel(
                            target_mode=target_mode,
                            sampled_damage=7,
                        ),
                        provenance,
                    ),
                    candidate_actor_guid=CANDIDATE,
                    target_guid_by_index=(TARGET_A, TARGET_B),
                    branch=_binding(
                        provenance,
                        branch_id=f"diagnostic-{target_mode}",
                        suffix_id="same-suffix",
                    ),
                )

                adapter.arm_next(TEAMMATE)
                with self.assertRaisesRegex(
                    ResponsiveTeamBridgeAdapterV1Error,
                    "diagnostic sampled target mode is not actionable",
                ):
                    adapter.emit_ready(TEAMMATE)

                self.assertEqual([], bridge.team_receipts)
                self.assertEqual([10.0, 20.0], bridge.health)
                self.assertEqual(
                    {TARGET_A: 10.0, TARGET_B: 20.0}, adapter.runtime.health
                )

    def test_actionable_sampler_is_used_when_runtime_store_exposes_it(self) -> None:
        class _ActionableModel(_FixedModel):
            def sample_emission(self, *, actor, emission_state, rng):
                raise AssertionError("raw diagnostic sampler must not drive the bridge")

            def sample_actionable_emission(self, *, actor, emission_state, rng):
                return _FixedModel.sample_emission(
                    self, actor=actor, emission_state=emission_state, rng=rng
                )

        provenance = _provenance()
        bridge = _ContractBridge(generation=1, config_digest="b" * 64)
        adapter = ResponsiveTeamBridgeAdapterV1(
            bridge=bridge,
            runtime=_runtime(),
            loaded_model=_loaded_model(_ActionableModel(), provenance),
            candidate_actor_guid=CANDIDATE,
            target_guid_by_index=(TARGET_A, TARGET_B),
            branch=_binding(
                provenance, branch_id="actionable-projection", suffix_id="same-suffix"
            ),
        )

        adapter.arm_next(TEAMMATE)
        result = adapter.emit_ready(TEAMMATE)
        self.assertEqual("STAY_ALIVE", result["sampled_emission"]["target_mode"])
        self.assertEqual("APPLIED", result["wire_receipt"]["status"])

    def test_all_living_targets_unattackable_consumes_wake_without_prefix_damage(
        self,
    ) -> None:
        provenance = _provenance()
        bridge = _ContractBridge(
            generation=1,
            config_digest="b" * 64,
            attackable=(False, False),
        )
        model = _FixedModel()
        adapter = ResponsiveTeamBridgeAdapterV1(
            bridge=bridge,
            runtime=_runtime(),
            loaded_model=_loaded_model(model, provenance),
            candidate_actor_guid=CANDIDATE,
            target_guid_by_index=(TARGET_A, TARGET_B),
            branch=_binding(
                provenance, branch_id="all-unattackable", suffix_id="same-suffix"
            ),
        )
        adapter.arm_next(TEAMMATE)
        result = adapter.emit_global_ready_and_rearm()

        self.assertEqual(
            "CANCELED_TARGET_UNATTACKABLE", result["emitted"]["wire_receipt"]["status"]
        )
        self.assertEqual(
            "UNATTACKABLE_ALIVE_CANCEL_WITNESS",
            result["emitted"]["target_selection_basis"],
        )
        self.assertEqual([10.0, 20.0], bridge.health)
        self.assertEqual({TARGET_A: 10.0, TARGET_B: 20.0}, adapter.runtime.health)
        self.assertFalse(
            result["emitted"]["local_runtime_transition"][
                "observed_in_model_prefix"
            ]
        )
        self.assertIsNotNone(result["next_wake"])
        self.assertEqual(200, result["next_wake"]["time_ms"])

    def test_multi_actor_scheduler_retains_deadlines_and_orders_same_ms_stably(
        self,
    ) -> None:
        provenance = _provenance()
        binding = _binding(
            provenance, branch_id="multi", suffix_id="candidate-kills-a"
        )
        bridge = _ContractBridge(generation=1, config_digest="b" * 64)
        adapter = ResponsiveTeamBridgeAdapterV1(
            bridge=bridge,
            runtime=_multi_actor_runtime(),
            loaded_model=_loaded_model(_FixedModel(), provenance),
            candidate_actor_guid=CANDIDATE,
            target_guid_by_index=(TARGET_A, TARGET_B),
            branch=binding,
        )

        first_wake = adapter.arm_global_next()
        self.assertIsNotNone(first_wake)
        self.assertEqual((100, TEAMMATE, 0), (
            first_wake["time_ms"],
            first_wake["actor_guid"],
            first_wake["event_sequence"],
        ))
        bridge.add_candidate_damage(time_ms=50, target_index=0, damage=10)

        first = adapter.emit_global_ready_and_rearm()
        self.assertEqual([100, TEAMMATE, 0], first["emitted"]["scheduler_order_key"])
        self.assertEqual(1, first["emitted"]["wire_event"]["target_index"])
        self.assertEqual(TEAMMATE_B, first["next_wake"]["actor_guid"])
        self.assertEqual(100, first["next_wake"]["time_ms"])
        self.assertEqual(0, first["next_wake"]["event_sequence"])

        second = adapter.emit_global_ready_and_rearm()
        self.assertEqual(
            [100, TEAMMATE_B, 0], second["emitted"]["scheduler_order_key"]
        )
        self.assertEqual(1, second["emitted"]["wire_event"]["target_index"])
        self.assertEqual(TEAMMATE, second["next_wake"]["actor_guid"])
        self.assertEqual(200, second["next_wake"]["time_ms"])
        self.assertEqual(1, second["next_wake"]["event_sequence"])
        self.assertEqual([0.0, 6.0], bridge.health)

    def test_scheduler_discards_unarmed_peer_deadlines_at_team_kill_clock(
        self,
    ) -> None:
        provenance = _provenance()
        bridge = _ContractBridge(
            generation=1,
            config_digest="b" * 64,
            target_health=(10.0, 7.0),
        )
        adapter = ResponsiveTeamBridgeAdapterV1(
            bridge=bridge,
            runtime=_multi_actor_runtime(target_health=(10, 7)),
            loaded_model=_loaded_model(_FixedModel(), provenance),
            candidate_actor_guid=CANDIDATE,
            target_guid_by_index=(TARGET_A, TARGET_B),
            branch=_binding(
                provenance, branch_id="kill-clock", suffix_id="candidate-kills-a"
            ),
        )
        adapter.arm_global_next()
        bridge.add_candidate_damage(time_ms=50, target_index=0, damage=10)
        step = adapter.emit_global_ready_and_rearm()

        self.assertEqual("EMITTED_TEAM_KILL_CLOCK_COMPLETE", step["status"])
        self.assertIsNone(step["next_wake"])
        self.assertEqual(0, step["retained_actor_deadline_count"])
        self.assertEqual(
            [TEAMMATE_B],
            [
                row["actor_guid"]
                for row in step["discarded_deadlines_after_team_kill_clock"]
            ],
        )

    def test_scheduler_keeps_running_when_only_future_target_remains(self) -> None:
        provenance = _provenance()
        bridge = _ContractBridge(
            generation=1,
            config_digest="b" * 64,
            target_health=(7.0, 9.0),
            attackable=(True, False),
        )
        runtime = DynamicTeamRuntimeV1(
            actors=(
                {
                    "player_guid": TEAMMATE,
                    "class": "MAGE",
                    "spec_key": "MAGE_SPEC_NOT_AVAILABLE",
                },
                {
                    "player_guid": CANDIDATE,
                    "class": "WARRIOR",
                    "spec_key": "WARRIOR_FURY",
                },
            ),
            target_health_by_guid={TARGET_A: 7, TARGET_B: 9},
            target_introduced_at_ms_by_guid={TARGET_A: 0, TARGET_B: 200},
        )
        runtime.actors[TEAMMATE].current_target_guid = TARGET_A
        adapter = ResponsiveTeamBridgeAdapterV1(
            bridge=bridge,
            runtime=runtime,
            loaded_model=_loaded_model(_FixedModel(), provenance),
            candidate_actor_guid=CANDIDATE,
            target_guid_by_index=(TARGET_A, TARGET_B),
            branch=_binding(
                provenance,
                branch_id="future-target-gap",
                suffix_id="first-target-dies-before-second-arrives",
            ),
        )

        adapter.arm_global_next()
        step = adapter.emit_global_ready_and_rearm()

        self.assertEqual("EMITTED_AND_NEXT_GLOBAL_WAKE_ARMED", step["status"])
        self.assertEqual(200, step["next_wake"]["time_ms"])
        self.assertEqual([], adapter.runtime.alive_target_guids())
        self.assertEqual([TARGET_B], adapter.runtime.remaining_target_guids())
        self.assertIsNone(adapter.runtime.kill_clock_ms)

    def test_exact_horizon_deadline_is_retained_as_discard_evidence_not_armed(self) -> None:
        provenance = _provenance()
        bridge = _ContractBridge(generation=1, config_digest="b" * 64)
        adapter = ResponsiveTeamBridgeAdapterV1(
            bridge=bridge,
            runtime=_runtime(),
            loaded_model=_loaded_model(_FixedModel(), provenance),
            candidate_actor_guid=CANDIDATE,
            target_guid_by_index=(TARGET_A, TARGET_B),
            branch=_binding(
                provenance,
                branch_id="exact-horizon",
                suffix_id="no-go-arm-call",
            ),
            wake_horizon_exclusive_ms=100,
        )

        self.assertIsNone(adapter.arm_global_next())
        self.assertIsNone(bridge.armed)
        evidence = adapter.horizon_discard_evidence()
        self.assertEqual(1, len(evidence))
        self.assertEqual(100, evidence[0]["time_ms"])
        self.assertEqual(100, evidence[0]["wake_horizon_exclusive_ms"])
        self.assertEqual(
            "wake_time_ms < wake_horizon_exclusive_ms",
            evidence[0]["boundary_semantics"],
        )
        self.assertFalse(evidence[0]["go_arm_dynamic_team_wake_called"])
        self.assertIsNone(adapter.arm_global_next())
        self.assertEqual(1, len(adapter.horizon_discard_evidence()))

    def test_rearm_at_exact_horizon_returns_explicit_terminal_evidence(self) -> None:
        provenance = _provenance()
        bridge = _ContractBridge(
            generation=1,
            config_digest="b" * 64,
            target_health=(100.0, 100.0),
        )
        adapter = ResponsiveTeamBridgeAdapterV1(
            bridge=bridge,
            runtime=_runtime(target_health=(100, 100)),
            loaded_model=_loaded_model(_FixedModel(), provenance),
            candidate_actor_guid=CANDIDATE,
            target_guid_by_index=(TARGET_A, TARGET_B),
            branch=_binding(
                provenance,
                branch_id="rearm-horizon",
                suffix_id="second-deadline-at-horizon",
            ),
            wake_horizon_exclusive_ms=200,
        )

        adapter.arm_global_next()
        step = adapter.emit_global_ready_and_rearm()

        self.assertEqual(
            "EMITTED_NO_NEXT_WAKE_BEFORE_EXCLUSIVE_HORIZON", step["status"]
        )
        self.assertIsNone(step["next_wake"])
        self.assertEqual(200, step["discarded_deadlines_at_or_after_horizon"][0]["time_ms"])
        self.assertIsNone(bridge.armed)

    def test_actor_rng_substream_does_not_depend_on_other_actor_sampling(self) -> None:
        provenance = _provenance()
        binding = _binding(
            provenance, branch_id="rng-substream", suffix_id="same-suffix"
        )

        def adapter() -> ResponsiveTeamBridgeAdapterV1:
            return ResponsiveTeamBridgeAdapterV1(
                bridge=_ContractBridge(generation=1, config_digest="b" * 64),
                runtime=_multi_actor_runtime(),
                loaded_model=_loaded_model(_RandomDelayModel(), provenance),
                candidate_actor_guid=CANDIDATE,
                target_guid_by_index=(TARGET_A, TARGET_B),
                branch=binding,
            )

        sampled_after_peer = adapter()
        sampled_after_peer._sample_actor_deadline(TEAMMATE_B)
        actor_deadline = sampled_after_peer._sample_actor_deadline(TEAMMATE)
        sampled_alone = adapter()._sample_actor_deadline(TEAMMATE)
        self.assertEqual(actor_deadline, sampled_alone)

    def test_pair_rejects_different_prefix(self) -> None:
        provenance = _provenance()
        left = _binding(provenance, branch_id="left", suffix_id="one")
        right = CausalBranchBindingV1(
            **{
                **_binding(provenance, branch_id="right", suffix_id="two").to_wire(),
                "prefix_content_sha256": "d" * 64,
            }
        )
        with self.assertRaisesRegex(
            ResponsiveTeamBridgeAdapterV1Error, "prefix_content_sha256"
        ):
            validate_matched_causal_pair_v1(left, right)

    @unittest.skipUnless(
        os.name == "nt" and OLD_WINDOWS_BRIDGE.is_file(),
        "old pinned Windows bridge is unavailable",
    )
    def test_real_v12_binary_proves_runtime_protocol_gap(self) -> None:
        bridge = SimulatorBridgeDynamicV3(OLD_WINDOWS_BRIDGE, cwd=SIMULATOR_ROOT)
        try:
            proof = probe_responsive_team_protocol_gap_v1(bridge)
            self.assertEqual(GAP_PROOF_SCHEMA, proof["schema"])
            self.assertEqual(
                "BLOCKED_GO_RESPONSIVE_TEAM_COMMANDS_ABSENT", proof["status"]
            )
            self.assertFalse(proof["dynamic_team_events_entered_go_lifecycle"])
        finally:
            bridge.close()
            if bridge._process.stdout is not None:
                bridge._process.stdout.close()
            if bridge._process.stderr is not None:
                bridge._process.stderr.close()

    @unittest.skipUnless(
        os.name == "nt" and RESPONSIVE_WINDOWS_BRIDGE.is_file(),
        "responsive Windows bridge is unavailable",
    )
    def test_real_v26_exact_horizon_deadline_is_not_sent_to_go(self) -> None:
        request = _two_target_request()
        request["encounter"]["duration"] = 0.1
        config = DynamicTargetSemanticsConfigV4(
            target_health=(
                DynamicTargetHealthV4(0, 100.0, 100.0),
                DynamicTargetHealthV4(1, 100.0, 100.0),
            ),
            idle_advance_horizon_ms=100,
        )
        bridge = SimulatorBridgeDynamicV4(
            RESPONSIVE_WINDOWS_BRIDGE, cwd=SIMULATOR_ROOT
        )
        try:
            loaded = bridge.load_dynamic_v4(request, seed=2026091301, config=config)
            provenance = _provenance()
            adapter = ResponsiveTeamBridgeAdapterV1(
                bridge=bridge,
                runtime=_runtime(target_health=(100, 100)),
                loaded_model=_loaded_model(_FixedModel(), provenance),
                candidate_actor_guid=CANDIDATE,
                target_guid_by_index=(TARGET_A, TARGET_B),
                branch=CausalBranchBindingV1(
                    pair_id="real-horizon-filter",
                    branch_id="exact-horizon",
                    candidate_suffix_id="no-responsive-wake",
                    prefix_content_sha256="4" * 64,
                    simulator_seed=2026091301,
                    teammate_seed=456,
                    environment_generation=loaded.receipt.environment_generation,
                    dynamic_config_sha256=config.content_sha256,
                    model_provenance_sha256=provenance.content_sha256,
                ),
                wake_horizon_exclusive_ms=config.idle_advance_horizon_ms,
            )

            self.assertIsNone(adapter.arm_global_next())
            state = bridge.state()
            self.assertIsNone(state.get("wake_ready"))
            self.assertIsNone(state.get("dynamic_team_response"))
            self.assertEqual(100, adapter.horizon_discard_evidence()[0]["time_ms"])
        finally:
            bridge.close()
            if bridge._process.stdout is not None:
                bridge._process.stdout.close()
            if bridge._process.stderr is not None:
                bridge._process.stderr.close()

    @unittest.skipUnless(
        os.name == "nt" and RESPONSIVE_WINDOWS_BRIDGE.is_file(),
        "responsive Windows bridge is unavailable",
    )
    def test_real_v26_bridge_retargets_after_candidate_kill_and_applies_event(self) -> None:
        config = DynamicTargetSemanticsConfigV4(
            target_health=(
                DynamicTargetHealthV4(0, 100.0, 100.0),
                DynamicTargetHealthV4(1, 100.0, 100.0),
            ),
            idle_advance_horizon_ms=2000,
        )
        bridge = SimulatorBridgeDynamicV4(
            RESPONSIVE_WINDOWS_BRIDGE, cwd=SIMULATOR_ROOT
        )
        try:
            loaded = bridge.load_dynamic_v4(
                _two_target_request(), seed=2026091301, config=config
            )
            provenance = _provenance()
            branch = CausalBranchBindingV1(
                pair_id="real-wire-smoke",
                branch_id="same-prefix-branch-a",
                candidate_suffix_id="simulator-candidate-autoattack-before-wake",
                prefix_content_sha256="f" * 64,
                simulator_seed=2026091301,
                teammate_seed=456,
                environment_generation=loaded.receipt.environment_generation,
                dynamic_config_sha256=config.content_sha256,
                model_provenance_sha256=provenance.content_sha256,
            )
            adapter = ResponsiveTeamBridgeAdapterV1(
                bridge=bridge,
                runtime=_runtime(target_health=(100, 100)),
                loaded_model=_loaded_model(_FixedModel(), provenance),
                candidate_actor_guid=CANDIDATE,
                target_guid_by_index=(TARGET_A, TARGET_B),
                branch=branch,
            )
            wake = adapter.arm_next(TEAMMATE)
            self.assertEqual(100, wake["time_ms"])
            bridge.wait(100)
            at_wake = bridge.advance()
            self.assertEqual(100, at_wake["time_ms"])
            self.assertEqual(wake["wake_id"], at_wake["wake_ready"]["wake_id"])
            result = adapter.emit_ready(TEAMMATE)
            # The simulator's time-zero candidate autoattack kills target 0.
            # Its authoritative candidate receipt enters the prefix before the
            # model samples, so STAY_ALIVE resolves to target 1 at the wake.
            self.assertGreater(adapter.candidate_cursor, 0)
            self.assertEqual(
                [TARGET_B],
                result["model_input_live_prefix"]["target_state"][
                    "alive_target_guids"
                ],
            )
            self.assertEqual(1, result["wire_event"]["target_index"])
            self.assertEqual(7.0, result["wire_receipt"]["applied_damage"])
            self.assertEqual(93.0, result["wire_receipt"]["current_health"])
            self.assertEqual(
                result["wire_receipt"], result["cursor_stream_receipt"]
            )
            self.assertFalse(result["comparison_eligible"])
        finally:
            bridge.close()
            if bridge._process.stdout is not None:
                bridge._process.stdout.close()
            if bridge._process.stderr is not None:
                bridge._process.stderr.close()

    @unittest.skipUnless(
        os.name == "nt" and ZERO_DMG_RESPONSIVE_WINDOWS_BRIDGE.is_file(),
        "zero-DMG responsive Windows bridge is unavailable",
    )
    def test_real_v26_untargeted_zero_damage_enters_prefix_with_zero_ordinal(
        self,
    ) -> None:
        self.assertEqual(
            ZERO_DMG_RESPONSIVE_WINDOWS_BRIDGE_SHA256,
            hashlib.sha256(ZERO_DMG_RESPONSIVE_WINDOWS_BRIDGE.read_bytes()).hexdigest(),
        )
        request = _two_target_request()
        for target in request["encounter"]["targets"]:
            target["stats"][34] = 1_000_000.0
        config = DynamicTargetSemanticsConfigV4(
            target_health=(
                DynamicTargetHealthV4(0, 1_000_000.0, 1_000_000.0),
                DynamicTargetHealthV4(1, 1_000_000.0, 1_000_000.0),
            ),
            idle_advance_horizon_ms=2000,
        )
        bridge = SimulatorBridgeDynamicV4(
            ZERO_DMG_RESPONSIVE_WINDOWS_BRIDGE, cwd=SIMULATOR_ROOT
        )
        try:
            loaded = bridge.load_dynamic_v4(request, 2026091306, config)
            provenance = _provenance()
            adapter = ResponsiveTeamBridgeAdapterV1(
                bridge=bridge,
                runtime=_runtime(target_health=(1_000_000, 1_000_000)),
                loaded_model=_loaded_model(
                    _UntargetedDmgModel(
                        target_mode="NO_TARGET",
                        sampled_damage=0,
                    ),
                    provenance,
                ),
                candidate_actor_guid=CANDIDATE,
                target_guid_by_index=(TARGET_A, TARGET_B),
                branch=CausalBranchBindingV1(
                    pair_id="real-untargeted-zero",
                    branch_id="untargeted-zero-branch",
                    candidate_suffix_id="candidate-live-prefix",
                    prefix_content_sha256="8" * 64,
                    simulator_seed=2026091306,
                    teammate_seed=456,
                    environment_generation=loaded.receipt.environment_generation,
                    dynamic_config_sha256=config.content_sha256,
                    model_provenance_sha256=provenance.content_sha256,
                ),
            )
            wake = adapter.arm_next(TEAMMATE)
            bridge.wait(wake["time_ms"])
            at_wake = bridge.advance()
            self.assertEqual(wake["wake_id"], at_wake["wake_ready"]["wake_id"])

            result = adapter.emit_ready(TEAMMATE)

            self.assertIsNone(result["wire_event"]["target_index"])
            self.assertEqual(0.0, result["wire_event"]["requested_damage"])
            self.assertEqual(
                "OBSERVED_NO_DAMAGE", result["wire_receipt"]["status"]
            )
            self.assertEqual(0, result["wire_receipt"]["damage_ordinal"])
            self.assertIsNone(result["wire_receipt"]["current_health"])
            self.assertEqual(
                "MODEL_EXPLICIT_UNTARGETED_ZERO_DAMAGE",
                result["target_selection_basis"],
            )
            self.assertTrue(
                result["local_runtime_transition"]["observed_in_model_prefix"]
            )
            self.assertEqual(
                result["wire_receipt"], result["cursor_stream_receipt"]
            )
            self.assertEqual(
                '["DMG",11722,"DIRECT_FRIENDLY_PLAYER"]',
                adapter.runtime._replay.actors[TEAMMATE].last_mark_token,
            )
        finally:
            bridge.close()
            if bridge._process.stdout is not None:
                bridge._process.stdout.close()
            if bridge._process.stderr is not None:
                bridge._process.stderr.close()

    @unittest.skipUnless(
        os.name == "nt" and RESPONSIVE_WINDOWS_BRIDGE.is_file(),
        "responsive Windows bridge is unavailable",
    )
    def test_real_v26_multi_actor_same_ms_retargets_and_rearms_stably(self) -> None:
        self.assertEqual(
            RESPONSIVE_WINDOWS_BRIDGE_SHA256,
            hashlib.sha256(RESPONSIVE_WINDOWS_BRIDGE.read_bytes()).hexdigest(),
        )
        config = DynamicTargetSemanticsConfigV4(
            target_health=(
                DynamicTargetHealthV4(0, 100.0, 100.0),
                DynamicTargetHealthV4(1, 100.0, 100.0),
            ),
            idle_advance_horizon_ms=2000,
        )
        bridge = SimulatorBridgeDynamicV4(
            RESPONSIVE_WINDOWS_BRIDGE, cwd=SIMULATOR_ROOT
        )
        try:
            loaded = bridge.load_dynamic_v4(
                _two_target_request(), seed=2026091301, config=config
            )
            provenance = _provenance()
            branch = CausalBranchBindingV1(
                pair_id="real-wire-multi",
                branch_id="same-prefix-multi-branch",
                candidate_suffix_id="simulator-candidate-autoattack-before-wake",
                prefix_content_sha256="9" * 64,
                simulator_seed=2026091301,
                teammate_seed=456,
                environment_generation=loaded.receipt.environment_generation,
                dynamic_config_sha256=config.content_sha256,
                model_provenance_sha256=provenance.content_sha256,
            )
            adapter = ResponsiveTeamBridgeAdapterV1(
                bridge=bridge,
                runtime=_multi_actor_runtime(target_health=(100, 100)),
                loaded_model=_loaded_model(_FixedModel(), provenance),
                candidate_actor_guid=CANDIDATE,
                target_guid_by_index=(TARGET_A, TARGET_B),
                branch=branch,
            )
            wake = adapter.arm_global_next()
            self.assertIsNotNone(wake)
            self.assertEqual((100, TEAMMATE, 0), (
                wake["time_ms"], wake["actor_guid"], wake["event_sequence"]
            ))
            bridge.wait(100)
            at_wake = bridge.advance()
            self.assertEqual(100, at_wake["time_ms"])

            first = adapter.emit_global_ready_and_rearm()
            self.assertEqual([100, TEAMMATE, 0], first["emitted"]["scheduler_order_key"])
            self.assertEqual(1, first["emitted"]["wire_event"]["target_index"])
            self.assertEqual(93.0, first["emitted"]["wire_receipt"]["current_health"])
            self.assertEqual(TEAMMATE_B, first["next_wake"]["actor_guid"])
            self.assertEqual(100, first["next_wake"]["time_ms"])

            second = adapter.emit_global_ready_and_rearm()
            self.assertEqual(
                [100, TEAMMATE_B, 0], second["emitted"]["scheduler_order_key"]
            )
            self.assertEqual(1, second["emitted"]["wire_event"]["target_index"])
            self.assertEqual(86.0, second["emitted"]["wire_receipt"]["current_health"])
            self.assertEqual(TEAMMATE, second["next_wake"]["actor_guid"])
            self.assertEqual(200, second["next_wake"]["time_ms"])
            self.assertEqual(1, second["next_wake"]["event_sequence"])
            self.assertEqual(
                [TARGET_B],
                first["emitted"]["model_input_live_prefix"]["target_state"][
                    "alive_target_guids"
                ],
            )
            self.assertEqual(
                [TARGET_B],
                second["emitted"]["model_input_live_prefix"]["target_state"][
                    "alive_target_guids"
                ],
            )
        finally:
            bridge.close()
            if bridge._process.stdout is not None:
                bridge._process.stdout.close()
            if bridge._process.stderr is not None:
                bridge._process.stderr.close()

    @unittest.skipUnless(
        os.name == "nt" and RESPONSIVE_WINDOWS_BRIDGE.is_file(),
        "responsive Windows bridge is unavailable",
    )
    def test_real_v26_fixed_background_receipt_enters_model_prefix(self) -> None:
        request = _two_target_request()
        for target in request["encounter"]["targets"]:
            target["stats"][34] = 1_000_000.0
        config = DynamicTargetSemanticsConfigV4(
            target_health=(
                DynamicTargetHealthV4(0, 1_000_000.0, 1_000_000.0),
                DynamicTargetHealthV4(1, 1_000_000.0, 1_000_000.0),
            ),
            background_damage_events=(
                BackgroundDamageEventV1(
                    0, 50, 0, "fixed-background-kills-target-0", 2_000_000.0
                ),
            ),
            idle_advance_horizon_ms=2000,
        )
        bridge = SimulatorBridgeDynamicV4(
            RESPONSIVE_WINDOWS_BRIDGE, cwd=SIMULATOR_ROOT
        )
        try:
            loaded = bridge.load_dynamic_v4(request, 2026091301, config)
            provenance = _provenance()
            branch = CausalBranchBindingV1(
                pair_id="real-fixed-prefix",
                branch_id="fixed-prefix-branch",
                candidate_suffix_id="candidate-autoattack",
                prefix_content_sha256="8" * 64,
                simulator_seed=2026091301,
                teammate_seed=456,
                environment_generation=loaded.receipt.environment_generation,
                dynamic_config_sha256=config.content_sha256,
                model_provenance_sha256=provenance.content_sha256,
            )
            adapter = ResponsiveTeamBridgeAdapterV1(
                bridge=bridge,
                runtime=_runtime(target_health=(1_000_000, 1_000_000)),
                loaded_model=_loaded_model(_FixedModel(), provenance),
                candidate_actor_guid=CANDIDATE,
                target_guid_by_index=(TARGET_A, TARGET_B),
                branch=branch,
            )
            wake = adapter.arm_next(TEAMMATE)
            bridge.wait(wake["time_ms"])
            bridge.advance()
            result = adapter.emit_ready(TEAMMATE)

            fixed = [
                row
                for row in result["authoritative_prefix_damage_receipts"]
                if row["source_kind"] == "FIXED_BACKGROUND"
            ]
            self.assertEqual(1, len(fixed))
            self.assertTrue(fixed[0]["runtime_transition"]["killed"])
            self.assertEqual(1, result["wire_event"]["target_index"])
            other = result["model_input_live_prefix"]["marked_activity"][
                "other_team_including_unattributed"
            ]
            self.assertGreater(other["damage_amount_3000ms"], 0)
            self.assertEqual(1, adapter.background_cursor)
        finally:
            bridge.close()
            if bridge._process.stdout is not None:
                bridge._process.stdout.close()
            if bridge._process.stderr is not None:
                bridge._process.stderr.close()

    @unittest.skipUnless(
        os.name == "nt" and RESPONSIVE_WINDOWS_BRIDGE.is_file(),
        "responsive Windows bridge is unavailable",
    )
    def test_real_v26_terminal_fixed_background_cancel_is_not_prefix_damage(
        self,
    ) -> None:
        request = _two_target_request()
        request["encounter"]["duration"] = 0.2
        request["encounter"]["targets"] = request["encounter"]["targets"][:1]
        request["encounter"]["targets"][0]["stats"][34] = 100.0
        config = DynamicTargetSemanticsConfigV4(
            target_health=(DynamicTargetHealthV4(0, 100.0, 20.0),),
            background_damage_events=(
                BackgroundDamageEventV1(0, 10, 0, "terminal-kill", 20.0),
                BackgroundDamageEventV1(1, 50, 0, "post-terminal", 1.0),
            ),
            idle_advance_horizon_ms=200,
        )
        bridge = SimulatorBridgeDynamicV4(
            RESPONSIVE_WINDOWS_BRIDGE, cwd=SIMULATOR_ROOT
        )
        try:
            loaded = bridge.load_dynamic_v4(request, 2026091304, config)
            provenance = _provenance()
            branch = CausalBranchBindingV1(
                pair_id="real-terminal-cancel",
                branch_id="terminal-cancel-branch",
                candidate_suffix_id="candidate-suspended",
                prefix_content_sha256="7" * 64,
                simulator_seed=2026091304,
                teammate_seed=456,
                environment_generation=loaded.receipt.environment_generation,
                dynamic_config_sha256=config.content_sha256,
                model_provenance_sha256=provenance.content_sha256,
            )
            adapter = ResponsiveTeamBridgeAdapterV1(
                bridge=bridge,
                runtime=_single_target_runtime(target_health=20.0),
                loaded_model=_loaded_model(_FixedModel(), provenance),
                candidate_actor_guid=CANDIDATE,
                target_guid_by_index=(TARGET_A,),
                branch=branch,
            )
            bridge.wait(20)
            terminal = bridge.advance()
            self.assertTrue(terminal["finished"])

            rows = adapter.sync_authoritative_damage_prefix()

            self.assertEqual((1, None), tuple(row["damage_ordinal"] for row in rows))
            self.assertEqual(0.0, adapter.runtime.health[TARGET_A])
            self.assertEqual(10, adapter.runtime.kill_clock_ms)
            self.assertTrue(rows[0]["mirrored_into_model_prefix"])
            self.assertFalse(rows[1]["mirrored_into_model_prefix"])
            self.assertEqual("CANCELED_TARGET_DEAD", rows[1]["status"])
            prefix = adapter.runtime.snapshot_for_actor(TEAMMATE)
            self.assertEqual(
                1,
                prefix["marked_activity"]["other_team_including_unattributed"][
                    "damage_event_count_3000ms"
                ],
            )
        finally:
            bridge.close()
            if bridge._process.stdout is not None:
                bridge._process.stdout.close()
            if bridge._process.stderr is not None:
                bridge._process.stderr.close()

    @unittest.skipUnless(
        os.name == "nt" and RESPONSIVE_WINDOWS_BRIDGE.is_file(),
        "responsive Windows bridge is unavailable",
    )
    def test_real_v26_excludes_alive_unattackable_target(self) -> None:
        request = _two_target_request()
        for target in request["encounter"]["targets"]:
            target["stats"][34] = 1_000_000.0
        config = DynamicTargetSemanticsConfigV4(
            target_health=(
                DynamicTargetHealthV4(0, 1_000_000.0, 1_000_000.0),
                DynamicTargetHealthV4(1, 1_000_000.0, 1_000_000.0),
            ),
            attackability_events=(DynamicAttackabilityEventV2(0, 50, 0, False),),
            idle_advance_horizon_ms=2000,
        )
        bridge = SimulatorBridgeDynamicV4(
            RESPONSIVE_WINDOWS_BRIDGE, cwd=SIMULATOR_ROOT
        )
        try:
            loaded = bridge.load_dynamic_v4(request, 2026091301, config)
            provenance = _provenance()
            branch = CausalBranchBindingV1(
                pair_id="real-attackability",
                branch_id="attackability-branch",
                candidate_suffix_id="candidate-autoattack",
                prefix_content_sha256="7" * 64,
                simulator_seed=2026091301,
                teammate_seed=456,
                environment_generation=loaded.receipt.environment_generation,
                dynamic_config_sha256=config.content_sha256,
                model_provenance_sha256=provenance.content_sha256,
            )
            adapter = ResponsiveTeamBridgeAdapterV1(
                bridge=bridge,
                runtime=_runtime(target_health=(1_000_000, 1_000_000)),
                loaded_model=_loaded_model(_FixedModel(), provenance),
                candidate_actor_guid=CANDIDATE,
                target_guid_by_index=(TARGET_A, TARGET_B),
                branch=branch,
            )
            wake = adapter.arm_next(TEAMMATE)
            bridge.wait(wake["time_ms"])
            bridge.advance()
            result = adapter.emit_ready(TEAMMATE)

            self.assertEqual(
                [0, 1], result["bridge_alive_target_indices_before_emission"]
            )
            self.assertEqual(
                [1],
                result[
                    "bridge_attackable_alive_target_indices_before_emission"
                ],
            )
            self.assertEqual(1, result["wire_event"]["target_index"])
            self.assertEqual("APPLIED", result["wire_receipt"]["status"])
        finally:
            bridge.close()
            if bridge._process.stdout is not None:
                bridge._process.stdout.close()
            if bridge._process.stderr is not None:
                bridge._process.stderr.close()

    @unittest.skipUnless(
        os.name == "nt" and RESPONSIVE_WINDOWS_BRIDGE.is_file(),
        "responsive Windows bridge is unavailable",
    )
    def test_real_v26_all_unattackable_consumes_wake_as_cancellation(self) -> None:
        request = _two_target_request()
        for target in request["encounter"]["targets"]:
            target["stats"][34] = 1_000_000.0
        config = DynamicTargetSemanticsConfigV4(
            target_health=(
                DynamicTargetHealthV4(0, 1_000_000.0, 1_000_000.0),
                DynamicTargetHealthV4(1, 1_000_000.0, 1_000_000.0),
            ),
            attackability_events=(
                DynamicAttackabilityEventV2(0, 50, 0, False),
                DynamicAttackabilityEventV2(1, 50, 1, False),
            ),
            idle_advance_horizon_ms=2000,
        )
        bridge = SimulatorBridgeDynamicV4(
            RESPONSIVE_WINDOWS_BRIDGE, cwd=SIMULATOR_ROOT
        )
        try:
            loaded = bridge.load_dynamic_v4(request, 2026091301, config)
            provenance = _provenance()
            branch = CausalBranchBindingV1(
                pair_id="real-all-unattackable",
                branch_id="all-unattackable-branch",
                candidate_suffix_id="candidate-autoattack",
                prefix_content_sha256="6" * 64,
                simulator_seed=2026091301,
                teammate_seed=456,
                environment_generation=loaded.receipt.environment_generation,
                dynamic_config_sha256=config.content_sha256,
                model_provenance_sha256=provenance.content_sha256,
            )
            adapter = ResponsiveTeamBridgeAdapterV1(
                bridge=bridge,
                runtime=_runtime(target_health=(1_000_000, 1_000_000)),
                loaded_model=_loaded_model(_FixedModel(), provenance),
                candidate_actor_guid=CANDIDATE,
                target_guid_by_index=(TARGET_A, TARGET_B),
                branch=branch,
            )
            wake = adapter.arm_next(TEAMMATE)
            bridge.wait(wake["time_ms"])
            bridge.advance()
            result = adapter.emit_ready(TEAMMATE)

            self.assertEqual(
                [], result["bridge_attackable_alive_target_indices_before_emission"]
            )
            self.assertEqual(
                "CANCELED_TARGET_UNATTACKABLE", result["wire_receipt"]["status"]
            )
            self.assertEqual(0.0, result["wire_receipt"]["applied_damage"])
            self.assertGreater(result["wire_receipt"]["damage_ordinal"], 0)
            self.assertEqual(
                "UNATTACKABLE_ALIVE_CANCEL_WITNESS",
                result["target_selection_basis"],
            )
            self.assertFalse(
                result["local_runtime_transition"]["observed_in_model_prefix"]
            )
        finally:
            bridge.close()
            if bridge._process.stdout is not None:
                bridge._process.stdout.close()
            if bridge._process.stderr is not None:
                bridge._process.stderr.close()

    @unittest.skipUnless(
        os.name == "nt" and RESPONSIVE_WINDOWS_BRIDGE.is_file(),
        "responsive Windows bridge is unavailable",
    )
    def test_real_v26_load_dynamic_v4_separates_maximum_and_current_health(self) -> None:
        request = _two_target_request()
        for target in request["encounter"]["targets"]:
            target["stats"][34] = 1_000_000.0
        dynamic = _dynamic_v4_wire(
            ((1_000_000.0, 500_000.0), (1_000_000.0, 750_000.0)),
            horizon_ms=2000,
        )
        bridge = SimulatorBridgeDynamicV3(
            RESPONSIVE_WINDOWS_BRIDGE, cwd=SIMULATOR_ROOT
        )
        try:
            response = bridge._request(
                "load_dynamic_v4",
                request=request,
                seed=2026091302,
                dynamic=dynamic,
            )
            self.assertEqual(dynamic["content_sha256"], response["dynamic_load"]["config_digest"])
            self.assertEqual(1_000_000.0, response["state"]["target_health_max"])
            first = response["state"]["dynamic_team_background"]["targets"][0]
            second = response["state"]["dynamic_team_background"]["targets"][1]
            self.assertEqual(500_000.0, first["initial_health"])
            self.assertEqual(750_000.0, second["initial_health"])
            self.assertEqual(
                first["initial_health"],
                first["current_health"] + first["simulated_damage_applied"],
            )
            self.assertEqual(
                second["initial_health"],
                second["current_health"] + second["simulated_damage_applied"],
            )
            self.assertLess(
                response["state"]["target_health"],
                response["state"]["target_health_max"],
            )
        finally:
            bridge.close()
            if bridge._process.stdout is not None:
                bridge._process.stdout.close()
            if bridge._process.stderr is not None:
                bridge._process.stderr.close()


if __name__ == "__main__":
    unittest.main()
