"""Development-only contract for a responsive teammate model on the Go bridge.

The v14 bridge supplements dynamic-v3/v4's target-bound load-time schedule
with a responsive two-stage protocol.  It can therefore represent a teammate
choosing a different living target after a candidate kills the historical
target early:

1. arm a simulator-owned wake for the sampled next teammate event time; and
2. at that wake, sample the mark and target from the then-current causal prefix,
   then submit the event through the Go dynamic-target damage lifecycle.

The adapter retains one absolute next-event deadline per actor, arms only the
globally earliest event, and uses a cursor-bound Go receipt as the authority for
every candidate and teammate health transition.  The legacy probe remains only
to document why v12 cannot run this adapter; it is not the current capability
status.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import math
import random
import re
from typing import Any, Mapping, Sequence

from .chronicle_external_teammate_response_model_v1 import (
    ABLATION_B,
    ABLATION_C,
    ABLATION_D,
    DynamicTeamRuntimeV1,
)
from .sim_bridge import SimBridgeCommandError


JSONMap = dict[str, Any]

ADAPTER_SCHEMA = "o2o_responsive_team_bridge_adapter/v1"
WAKE_SCHEMA = "o2o_dynamic_team_wake/v1"
EVENT_SCHEMA = "o2o_dynamic_team_event/v1"
EVENT_RECEIPT_SCHEMA = "o2o_dynamic_team_response_receipt/v1"
EVENT_RECEIPTS_SCHEMA = "o2o_dynamic_team_response_receipts/v1"
GAP_PROOF_SCHEMA = "o2o_responsive_team_bridge_gap_proof/v1"

ARM_WAKE_COMMAND = "arm_dynamic_team_wake"
EMIT_EVENT_COMMAND = "emit_dynamic_team_event"
EVENT_RECEIPTS_COMMAND = "dynamic_team_response_receipts"
REQUIRED_COMMANDS = (
    ARM_WAKE_COMMAND,
    EMIT_EVENT_COMMAND,
    EVENT_RECEIPTS_COMMAND,
)
SAME_TIMESTAMP_ORDER = (
    "TARGET_SEMANTICS_BEFORE_FIXED_BACKGROUND_BEFORE_RESPONSIVE_TEAM_WAKE_"
    "BEFORE_ENVIRONMENT_WAKE_BEFORE_CANDIDATE"
)
_SHA256 = re.compile(r"[0-9a-f]{64}")
_EVENT_TYPES = frozenset({"START", "GO", "FAIL", "DMG", "HEAL"})
_EVENT_STATUSES = frozenset(
    {"APPLIED", "OBSERVED_NO_DAMAGE", "CANCELED_TARGET_UNATTACKABLE"}
)


class ResponsiveTeamBridgeAdapterV1Error(RuntimeError):
    """The responsive-team bridge contract or causal ordering was violated."""


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ResponsiveTeamBridgeAdapterV1Error(f"{label} must be nonempty text")
    return value


def _sha256(value: Any, label: str) -> str:
    text = _text(value, label)
    if _SHA256.fullmatch(text) is None:
        raise ResponsiveTeamBridgeAdapterV1Error(f"{label} must be lowercase SHA-256")
    return text


def _apply_unattributed_damage(
    runtime: DynamicTeamRuntimeV1,
    *,
    time_ms: int,
    target_guid: str,
    applied_damage: int | float,
    source_name: str,
) -> JSONMap:
    """Mirror an authoritative fixed-background receipt in adapter state."""

    now = _integer(time_ms, "runtime unattributed damage time_ms")
    if now < runtime.time_ms:
        raise ResponsiveTeamBridgeAdapterV1Error(
            "runtime unattributed damage time regressed"
        )
    target = _text(target_guid, "runtime unattributed damage target guid")
    if target not in runtime.health:
        raise ResponsiveTeamBridgeAdapterV1Error(
            "runtime unattributed damage target is unknown"
        )
    source = _text(source_name, "source_name")
    damage = float(applied_damage)
    if (
        not math.isfinite(damage)
        or damage <= 0
        or damage > runtime.health[target]
    ):
        raise ResponsiveTeamBridgeAdapterV1Error(
            "runtime unattributed applied damage differs from live health"
        )
    runtime.health[target] -= damage
    killed = runtime.health[target] <= 0
    runtime.time_ms = now
    runtime._replay.observe(
        {
            "trace_kind": "UNATTRIBUTED_EVENT",
            "anchor": {"offset_ms": now},
            "event": {
                "event_type": "DMG",
                "source": {"guid": None, "lane": "UNKNOWN"},
                "target": {
                    "guid": target,
                    "lane": "HOSTILE_CREATURE",
                    "voting_enemy_target": True,
                },
                "spell": {"id": None, "name": source},
                "attribution": {
                    "attribution_kind": "UNATTRIBUTED",
                    "player_guid": None,
                },
                "damage": {
                    "amount": int(round(damage)),
                    "amount_source": "AUTHORITATIVE_BACKGROUND_RECEIPT",
                },
            },
        }
    )
    retargets: list[JSONMap] = []
    if killed:
        runtime._replay._observe_target(
            target, "HOSTILE_CREATURE", now, dead=True
        )
        for actor_guid, actor in sorted(runtime.actors.items()):
            if actor.current_target_guid == target:
                actor.current_target_guid = None
                retargets.append(
                    {
                        "actor_guid": actor_guid,
                        "dead_target_guid": target,
                        "retargeted_to": None,
                    }
                )
        if not runtime.alive_target_guids():
            runtime.kill_clock_ms = now
    return {
        "time_ms": now,
        "actor_guid": None,
        "actor_role": "FIXED_BACKGROUND_AUTHORITATIVE_PREFIX",
        "event_type": "DMG",
        "target_guid": target,
        "requested_damage": damage,
        "applied_damage": damage,
        "overkill_damage": 0.0,
        "killed": killed,
        "retargets": retargets,
        "all_targets_dead": runtime.kill_clock_ms is not None,
        "kill_clock_ms": runtime.kill_clock_ms,
    }


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ResponsiveTeamBridgeAdapterV1Error(
            f"{label} must be an integer >= {minimum}"
        )
    return value


def _number(value: Any, label: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResponsiveTeamBridgeAdapterV1Error(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise ResponsiveTeamBridgeAdapterV1Error(
            f"{label} must be finite and >= {minimum}"
        )
    return result


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResponsiveTeamBridgeAdapterV1Error(f"{label} must be an object")
    return value


@dataclass(frozen=True)
class TeammateModelProvenanceV1:
    """Identity and scientific scope of the already-materialized model."""

    source_artifact_schema: str
    source_artifact_content_sha256: str
    model_content_sha256: str
    variant_id: str
    training_scope: str
    current_source_held_out: bool
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _text(self.source_artifact_schema, "model source_artifact_schema")
        _sha256(
            self.source_artifact_content_sha256,
            "model source_artifact_content_sha256",
        )
        _sha256(self.model_content_sha256, "model_content_sha256")
        if self.variant_id not in {ABLATION_B, ABLATION_C, ABLATION_D}:
            raise ResponsiveTeamBridgeAdapterV1Error(
                "responsive runtime requires a learned B/C/D teammate model"
            )
        _text(self.training_scope, "model training_scope")
        if not isinstance(self.current_source_held_out, bool):
            raise ResponsiveTeamBridgeAdapterV1Error(
                "current_source_held_out must be boolean"
            )
        object.__setattr__(self, "content_sha256", _canonical_sha256(self.to_wire()))

    def to_wire(self) -> JSONMap:
        return {
            "source_artifact_schema": self.source_artifact_schema,
            "source_artifact_content_sha256": self.source_artifact_content_sha256,
            "model_content_sha256": self.model_content_sha256,
            "model_content_scope": "canonical selected serialized B/C/D runtime model",
            "variant_id": self.variant_id,
            "training_scope": self.training_scope,
            "current_source_held_out": self.current_source_held_out,
            "development_only": True,
            "comparison_eligible": False,
            "voting_eligible": False,
            "deployment_eligible": False,
        }


@dataclass(frozen=True)
class LoadedResponsiveTeammateModelV1:
    """One atomically validated model, artifact identity, and source claim."""

    model: Any
    provenance: TeammateModelProvenanceV1
    result_content_sha256: str
    current_source_evidence: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.provenance, TeammateModelProvenanceV1):
            raise ResponsiveTeamBridgeAdapterV1Error(
                "loaded teammate model requires validated provenance"
            )
        result_sha = _sha256(
            self.result_content_sha256, "loaded teammate result_content_sha256"
        )
        if result_sha != self.provenance.source_artifact_content_sha256:
            raise ResponsiveTeamBridgeAdapterV1Error(
                "loaded teammate result identity differs from provenance"
            )
        if not isinstance(self.current_source_evidence, Mapping):
            raise ResponsiveTeamBridgeAdapterV1Error(
                "loaded teammate current_source_evidence must be an object"
            )
        if getattr(self.model, "variant_id", None) != self.provenance.variant_id:
            raise ResponsiveTeamBridgeAdapterV1Error(
                "loaded teammate model variant differs from provenance"
            )
        if (
            getattr(self.model, "model_content_sha256", None)
            != self.provenance.model_content_sha256
        ):
            raise ResponsiveTeamBridgeAdapterV1Error(
                "loaded teammate model content identity differs from provenance"
            )
        if not callable(getattr(self.model, "sample_delay", None)) or not callable(
            getattr(self.model, "sample_emission", None)
        ):
            raise ResponsiveTeamBridgeAdapterV1Error(
                "loaded teammate model lacks the responsive sampling surface"
            )


@dataclass(frozen=True)
class CausalBranchBindingV1:
    """Common-prefix identity plus the branch-specific candidate suffix."""

    pair_id: str
    branch_id: str
    candidate_suffix_id: str
    prefix_content_sha256: str
    simulator_seed: int
    teammate_seed: int
    environment_generation: int
    dynamic_config_sha256: str
    model_provenance_sha256: str
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        for value, label in (
            (self.pair_id, "pair_id"),
            (self.branch_id, "branch_id"),
            (self.candidate_suffix_id, "candidate_suffix_id"),
        ):
            _text(value, label)
        _sha256(self.prefix_content_sha256, "prefix_content_sha256")
        _sha256(self.dynamic_config_sha256, "dynamic_config_sha256")
        _sha256(self.model_provenance_sha256, "model_provenance_sha256")
        _integer(self.simulator_seed, "simulator_seed")
        _integer(self.teammate_seed, "teammate_seed")
        _integer(self.environment_generation, "environment_generation", minimum=1)
        object.__setattr__(self, "content_sha256", _canonical_sha256(self.to_wire()))

    def to_wire(self) -> JSONMap:
        return {
            "pair_id": self.pair_id,
            "branch_id": self.branch_id,
            "candidate_suffix_id": self.candidate_suffix_id,
            "prefix_content_sha256": self.prefix_content_sha256,
            "simulator_seed": self.simulator_seed,
            "teammate_seed": self.teammate_seed,
            "environment_generation": self.environment_generation,
            "dynamic_config_sha256": self.dynamic_config_sha256,
            "model_provenance_sha256": self.model_provenance_sha256,
        }


def validate_matched_causal_pair_v1(
    left: CausalBranchBindingV1, right: CausalBranchBindingV1
) -> JSONMap:
    """Require one observed prefix and distinct candidate-controlled suffixes."""

    common_fields = (
        "pair_id",
        "prefix_content_sha256",
        "simulator_seed",
        "teammate_seed",
        "dynamic_config_sha256",
        "model_provenance_sha256",
    )
    differing = [
        field for field in common_fields if getattr(left, field) != getattr(right, field)
    ]
    if differing:
        raise ResponsiveTeamBridgeAdapterV1Error(
            "causal pair common-prefix binding differs: " + ", ".join(differing)
        )
    if left.branch_id == right.branch_id:
        raise ResponsiveTeamBridgeAdapterV1Error("causal pair branch_id must differ")
    if left.candidate_suffix_id == right.candidate_suffix_id:
        raise ResponsiveTeamBridgeAdapterV1Error(
            "causal pair candidate_suffix_id must differ"
        )
    return {
        "schema": f"{ADAPTER_SCHEMA}/matched_causal_pair",
        "status": "MATCHED_PREFIX_DISTINCT_CANDIDATE_SUFFIX",
        "pair_id": left.pair_id,
        "prefix_content_sha256": left.prefix_content_sha256,
        "simulator_seed": left.simulator_seed,
        "teammate_seed": left.teammate_seed,
        "branch_ids": [left.branch_id, right.branch_id],
        "candidate_suffix_ids": [
            left.candidate_suffix_id,
            right.candidate_suffix_id,
        ],
        "future_candidate_suffix_visible_to_teammate_model": False,
        "realized_candidate_prefix_effects_visible_to_teammate_model": True,
        "comparison_eligible": False,
        "deployment_eligible": False,
    }


def required_go_protocol_contract_v1() -> JSONMap:
    """Machine-readable contract needed before a real bridge run can exist."""

    return {
        "schema": ADAPTER_SCHEMA + "/required_go_protocol",
        "required_commands": list(REQUIRED_COMMANDS),
        "same_timestamp_order": SAME_TIMESTAMP_ORDER,
        "wake_contract": {
            "simulator_owns_deadline": True,
            "pause_at_exact_deadline_before_candidate": True,
            "state_exposes_bound_wake_id": True,
            "allows_rearm_while_policy_input_is_suspended": True,
        },
        "emit_contract": {
            "requires_current_simulator_time": True,
            "python_selects_from_live_alive_and_attackable_registry_while_simulator_paused": True,
            "go_receipt_reports_dead_or_unattackable_cancellation": True,
            "go_applies_damage_through_dynamic_target_lifecycle": True,
            "receipt_includes_damage_ordinal_health_and_death": True,
        },
        "scientific_boundary": {
            "model_receives_live_prefix_only": True,
            "candidate_policy_identity_visible_to_model": False,
            "fixed_future_target_schedule": False,
            "development_only": True,
            "comparison_eligible": False,
            "voting_eligible": False,
            "deployment_eligible": False,
        },
    }


def probe_responsive_team_protocol_gap_v1(bridge: Any) -> JSONMap:
    """Probe an old JSONL surface without loading or mutating an environment."""

    try:
        bridge._request(
            ARM_WAKE_COMMAND,
            responsive={
                "schema": WAKE_SCHEMA,
                "model_content_sha256": "0" * 64,
                "wake_id": "protocol-probe",
                "time_ms": 1,
            },
        )
    except SimBridgeCommandError as error:
        message = str(error)
        absent = f'unknown command "{ARM_WAKE_COMMAND}"'
        if absent in message:
            return {
                "schema": GAP_PROOF_SCHEMA,
                "status": "BLOCKED_GO_RESPONSIVE_TEAM_COMMANDS_ABSENT",
                "probe_command": ARM_WAKE_COMMAND,
                "bridge_rejection": absent,
                "required_commands": list(REQUIRED_COMMANDS),
                "dynamic_team_events_entered_go_lifecycle": False,
                "comparison_eligible": False,
                "voting_eligible": False,
                "deployment_eligible": False,
            }
        # A recognized command is expected to reject the probe because no
        # environment has been loaded.  Any other rejection is still direct
        # evidence that the command reached a handler rather than default.
        return {
            "schema": GAP_PROOF_SCHEMA,
            "status": "GO_RESPONSIVE_TEAM_ARM_COMMAND_RECOGNIZED",
            "probe_command": ARM_WAKE_COMMAND,
            "bridge_rejection": message,
            "required_commands": list(REQUIRED_COMMANDS),
            "dynamic_team_events_entered_go_lifecycle": False,
            "comparison_eligible": False,
            "voting_eligible": False,
            "deployment_eligible": False,
        }
    return {
        "schema": GAP_PROOF_SCHEMA,
        "status": "INVALID_PROTOCOL_PROBE_ACCEPTED_WITHOUT_ENVIRONMENT",
        "probe_command": ARM_WAKE_COMMAND,
        "required_commands": list(REQUIRED_COMMANDS),
        "dynamic_team_events_entered_go_lifecycle": False,
        "comparison_eligible": False,
        "voting_eligible": False,
        "deployment_eligible": False,
    }


def _substream_seed(seed: int, actor_guid: str, sequence: int, phase: str) -> int:
    # Per-event substreams keep a retarget draw in one candidate branch from
    # shifting the next teammate mark/delay draw in its matched branch.
    payload = f"{seed}\0{actor_guid}\0{sequence}\0{phase}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def _model_actor_identity(runtime: DynamicTeamRuntimeV1, actor_guid: str) -> JSONMap:
    """Expose only the three actor fields used by the learned model."""

    metadata = runtime.actors[actor_guid].metadata
    return {
        "player_guid": _text(metadata.get("player_guid"), "actor player_guid"),
        "class": _text(metadata.get("class"), "actor class"),
        "spec_key": _text(metadata.get("spec_key"), "actor spec_key"),
    }


def _resolve_eligible_target(
    runtime: DynamicTeamRuntimeV1,
    *,
    actor_guid: str,
    target_mode: str,
    eligible_target_guids: Sequence[str],
    rng: random.Random,
) -> str | None:
    eligible = sorted(set(eligible_target_guids))
    if not eligible or target_mode in {"NO_TARGET", "NON_HOSTILE_OR_UNKNOWN"}:
        return None
    current = runtime.actors[actor_guid].current_target_guid
    if target_mode == "STAY_ALIVE" and current in eligible:
        return current
    alternatives = [guid for guid in eligible if guid != current]
    if target_mode == "SWITCH_ALIVE" and alternatives:
        return alternatives[rng.randrange(len(alternatives))]
    if current in eligible:
        return current
    return eligible[rng.randrange(len(eligible))]


class ResponsiveTeamBridgeAdapterV1:
    """Drive a learned teammate model against the implemented two-stage wire API."""

    def __init__(
        self,
        *,
        bridge: Any,
        runtime: DynamicTeamRuntimeV1,
        loaded_model: LoadedResponsiveTeammateModelV1,
        candidate_actor_guid: str,
        target_guid_by_index: Sequence[str],
        branch: CausalBranchBindingV1,
    ) -> None:
        if not isinstance(loaded_model, LoadedResponsiveTeammateModelV1):
            raise ResponsiveTeamBridgeAdapterV1Error(
                "adapter requires one LoadedResponsiveTeammateModelV1 binding"
            )
        model = loaded_model.model
        model_provenance = loaded_model.provenance
        if getattr(model, "variant_id", None) != model_provenance.variant_id:
            raise ResponsiveTeamBridgeAdapterV1Error(
                "loaded teammate model variant changed after validation"
            )
        if (
            getattr(model, "model_content_sha256", None)
            != model_provenance.model_content_sha256
        ):
            raise ResponsiveTeamBridgeAdapterV1Error(
                "loaded teammate model content identity changed after validation"
            )
        if branch.model_provenance_sha256 != model_provenance.content_sha256:
            raise ResponsiveTeamBridgeAdapterV1Error(
                "branch is not bound to the supplied teammate model provenance"
            )
        candidate = _text(candidate_actor_guid, "candidate_actor_guid")
        if candidate not in runtime.actors:
            raise ResponsiveTeamBridgeAdapterV1Error(
                "candidate actor is absent from DynamicTeamRuntimeV1"
            )
        targets = tuple(_text(value, "target guid") for value in target_guid_by_index)
        if len(set(targets)) != len(targets) or set(targets) != set(runtime.health):
            raise ResponsiveTeamBridgeAdapterV1Error(
                "target index registry differs from DynamicTeamRuntimeV1"
            )
        self.bridge = bridge
        self.runtime = runtime
        self.loaded_model = loaded_model
        self.model = model
        self.candidate_actor_guid = candidate
        self.target_guid_by_index = targets
        self.target_index_by_guid = {
            guid: index for index, guid in enumerate(self.target_guid_by_index)
        }
        self.model_provenance = model_provenance
        self.branch = branch
        self.background_cursor = 0
        self.candidate_cursor = 0
        self.team_response_cursor = 0
        self._sequence_by_actor = {guid: 0 for guid in runtime.actors if guid != candidate}
        self._deadline_by_actor: dict[str, JSONMap] = {}
        self._armed_by_actor: dict[str, JSONMap] = {}
        self._active_actor: str | None = None

    def _validate_damage_batch(
        self, batch: Any, *, cursor: int, label: str
    ) -> None:
        if batch.environment_generation != self.branch.environment_generation:
            raise ResponsiveTeamBridgeAdapterV1Error(
                f"{label} receipt generation differs from branch"
            )
        if batch.config_digest != self.branch.dynamic_config_sha256:
            raise ResponsiveTeamBridgeAdapterV1Error(
                f"{label} receipt config differs from branch"
            )
        if batch.cursor != cursor or batch.next_cursor != cursor + len(batch.receipts):
            raise ResponsiveTeamBridgeAdapterV1Error(
                f"{label} receipt cursor differs from request"
            )

    def sync_authoritative_damage_prefix(self) -> tuple[JSONMap, ...]:
        """Merge fixed-background and candidate HP receipts in ledger order."""

        background_batch = self.bridge.dynamic_damage_receipts(
            cursor=self.background_cursor
        )
        candidate_batch = self.bridge.dynamic_candidate_damage_receipts(
            cursor=self.candidate_cursor
        )
        self._validate_damage_batch(
            background_batch, cursor=self.background_cursor, label="fixed background"
        )
        self._validate_damage_batch(
            candidate_batch, cursor=self.candidate_cursor, label="candidate"
        )

        rows: list[JSONMap] = []
        for source_rank, (source_kind, receipts) in enumerate(
            (
                ("FIXED_BACKGROUND", background_batch.receipts),
                ("CANDIDATE", candidate_batch.receipts),
            )
        ):
            for source_sequence, receipt in enumerate(receipts):
                target_index = _integer(
                    receipt.target_index, f"{source_kind} target_index"
                )
                if target_index >= len(self.target_guid_by_index):
                    raise ResponsiveTeamBridgeAdapterV1Error(
                        f"{source_kind} receipt target is outside registry"
                    )
                applied = _number(
                    receipt.applied_damage, f"{source_kind} applied_damage"
                )
                status = getattr(receipt, "status", None)
                raw_ordinal = receipt.damage_ordinal
                if raw_ordinal is None:
                    if not (
                        source_kind == "FIXED_BACKGROUND"
                        and applied == 0
                        and status == "CANCELED_TARGET_DEAD"
                    ):
                        raise ResponsiveTeamBridgeAdapterV1Error(
                            f"{source_kind} receipt lacks a damage ledger ordinal"
                        )
                    ordinal = None
                else:
                    ordinal = _integer(
                        raw_ordinal, f"{source_kind} damage_ordinal"
                    )
                if applied > 0 and (ordinal is None or ordinal <= 0):
                    raise ResponsiveTeamBridgeAdapterV1Error(
                        f"{source_kind} applied damage lacks a ledger ordinal"
                    )
                rows.append(
                    {
                        "source_kind": source_kind,
                        "source_rank": source_rank,
                        "source_sequence": source_sequence,
                        "damage_ordinal": ordinal,
                        "status": status,
                        "time_ms": _integer(
                            receipt.time_ms, f"{source_kind} time_ms"
                        ),
                        "target_index": target_index,
                        "applied_damage": applied,
                        "event_id": getattr(receipt, "event_id", None),
                        "spell_id": getattr(
                            getattr(receipt, "action", None), "spell_id", None
                        ),
                        "mirrored_into_model_prefix": applied > 0,
                    }
                )
        positive_ordinals = [
            row["damage_ordinal"] for row in rows if row["applied_damage"] > 0
        ]
        if len(positive_ordinals) != len(set(positive_ordinals)):
            raise ResponsiveTeamBridgeAdapterV1Error(
                "authoritative damage streams repeat a positive ledger ordinal"
            )
        rows.sort(
            key=lambda row: (
                row["time_ms"],
                row["damage_ordinal"]
                if row["damage_ordinal"] is not None
                and row["damage_ordinal"] > 0
                else 1 << 63,
                row["source_rank"],
                row["source_sequence"],
            )
        )
        tentative = deepcopy(self.runtime)
        for row in rows:
            if row["applied_damage"] <= 0:
                continue
            target_guid = self.target_guid_by_index[row["target_index"]]
            if row["source_kind"] == "FIXED_BACKGROUND":
                transition = _apply_unattributed_damage(
                    tentative,
                    time_ms=row["time_ms"],
                    target_guid=target_guid,
                    applied_damage=row["applied_damage"],
                    source_name=row["event_id"] or "fixed-background-damage",
                )
            else:
                transition = tentative.apply_event(
                    time_ms=row["time_ms"],
                    actor_guid=self.candidate_actor_guid,
                    event_type="DMG",
                    spell_id=row["spell_id"],
                    spell_name="candidate-bridge-damage",
                    attribution_kind="DIRECT_FRIENDLY_PLAYER",
                    exact_source_guid=self.candidate_actor_guid,
                    target_mode="STAY_ALIVE",
                    requested_damage=row["applied_damage"],
                    rng=random.Random(0),
                    actor_role="CANDIDATE_REALIZED_PREFIX",
                    explicit_target_guid=target_guid,
                )
            if transition["applied_damage"] != row["applied_damage"]:
                raise ResponsiveTeamBridgeAdapterV1Error(
                    "authoritative damage health transition differs from runtime mirror"
                )
            row["runtime_transition"] = transition
        self.runtime = tentative
        self.background_cursor = background_batch.next_cursor
        self.candidate_cursor = candidate_batch.next_cursor
        for row in rows:
            row.pop("source_rank", None)
            row.pop("source_sequence", None)
        return tuple(rows)

    def _sample_actor_deadline(self, actor_guid: str) -> JSONMap:
        actor = _text(actor_guid, "teammate actor_guid")
        if actor not in self._sequence_by_actor:
            raise ResponsiveTeamBridgeAdapterV1Error("unknown teammate actor")
        if actor in self._deadline_by_actor:
            raise ResponsiveTeamBridgeAdapterV1Error(
                "teammate actor already has a planned deadline"
            )
        if not self.runtime.alive_target_guids():
            raise ResponsiveTeamBridgeAdapterV1Error(
                "cannot plan a teammate event after the team kill clock"
            )
        sequence = self._sequence_by_actor[actor]
        actor_identity = _model_actor_identity(self.runtime, actor)
        sampled = self.model.sample_delay(
            actor=actor_identity,
            timing_state=self.runtime.snapshot_for_actor(actor),
            rng=random.Random(
                _substream_seed(self.branch.teammate_seed, actor, sequence, "delay")
            ),
        )
        delay_ms = _integer(sampled.get("delay_ms"), "sampled delay")
        wake_id = f"wake-{self.branch.content_sha256}-{actor}-{sequence}"
        wake = {
            "schema": WAKE_SCHEMA,
            "model_content_sha256": self.model_provenance.model_content_sha256,
            "wake_id": wake_id,
            "time_ms": self.runtime.time_ms + delay_ms,
        }
        delay_sample = {
            "delay_ms": delay_ms,
            "delay_bucket": _integer(
                sampled.get("delay_bucket"), "sampled delay bucket"
            ),
            "context_level": _text(
                sampled.get("context_level"), "delay context level"
            ),
            "support": _integer(sampled.get("support"), "delay support", minimum=1),
        }
        deadline = {
            "actor_guid": actor,
            "event_sequence": sequence,
            "time_ms": wake["time_ms"],
            "wake_id": wake_id,
            "delay_sample": delay_sample,
        }
        self._deadline_by_actor[actor] = deadline
        return dict(deadline)

    def plan_all_actor_deadlines(self) -> tuple[JSONMap, ...]:
        """Fill missing per-actor clocks without resampling retained deadlines."""

        if self._active_actor is not None:
            raise ResponsiveTeamBridgeAdapterV1Error(
                "cannot initialize actor deadlines while a wake is active"
            )
        self.sync_authoritative_damage_prefix()
        if not self.runtime.alive_target_guids():
            return ()
        for actor in sorted(self._sequence_by_actor):
            if actor not in self._deadline_by_actor:
                self._sample_actor_deadline(actor)
        return tuple(
            dict(row)
            for row in sorted(
                self._deadline_by_actor.values(),
                key=self._deadline_order_key,
            )
        )

    @staticmethod
    def _deadline_order_key(row: Mapping[str, Any]) -> tuple[int, str, int]:
        return (
            int(row["time_ms"]),
            str(row["actor_guid"]),
            int(row["event_sequence"]),
        )

    def _arm_planned_actor(self, actor: str) -> JSONMap:
        if self._active_actor is not None:
            raise ResponsiveTeamBridgeAdapterV1Error(
                "the bridge already has an active responsive wake"
            )
        deadline = self._deadline_by_actor.get(actor)
        if deadline is None:
            raise ResponsiveTeamBridgeAdapterV1Error(
                "teammate actor has no planned deadline"
            )
        wake = {
            "schema": WAKE_SCHEMA,
            "model_content_sha256": self.model_provenance.model_content_sha256,
            "wake_id": deadline["wake_id"],
            "time_ms": deadline["time_ms"],
        }
        response = self.bridge._request(ARM_WAKE_COMMAND, responsive=wake)
        receipt = _mapping(response.get("responsive_team_wake"), "wake receipt")
        expected = {
            "schema": WAKE_SCHEMA,
            "model_content_sha256": self.model_provenance.model_content_sha256,
            "wake_id": wake["wake_id"],
            "time_ms": wake["time_ms"],
            "status": "ARMED",
        }
        if dict(receipt) != expected:
            raise ResponsiveTeamBridgeAdapterV1Error(
                "responsive teammate wake receipt differs from request binding"
            )
        self._armed_by_actor[actor] = {
            **expected,
            "actor_guid": actor,
            "event_sequence": deadline["event_sequence"],
            "delay_sample": deadline["delay_sample"],
        }
        self._active_actor = actor
        return dict(self._armed_by_actor[actor])

    def arm_next(self, actor_guid: str) -> JSONMap:
        """Compatibility entry point for a single explicitly selected actor."""

        if self._deadline_by_actor:
            raise ResponsiveTeamBridgeAdapterV1Error(
                "explicit actor arming cannot bypass existing global deadlines"
            )
        self.sync_authoritative_damage_prefix()
        deadline = self._sample_actor_deadline(actor_guid)
        return self._arm_planned_actor(str(deadline["actor_guid"]))

    def arm_global_next(self) -> JSONMap | None:
        """Arm the earliest retained absolute deadline across all actors."""

        if self._active_actor is not None:
            raise ResponsiveTeamBridgeAdapterV1Error(
                "the bridge already has an active responsive wake"
            )
        if not self._deadline_by_actor:
            self.plan_all_actor_deadlines()
        if not self._deadline_by_actor:
            return None
        selected = min(
            self._deadline_by_actor.values(), key=self._deadline_order_key
        )
        return self._arm_planned_actor(str(selected["actor_guid"]))

    def emit_ready(self, actor_guid: str) -> JSONMap:
        """Sample at the due wake and apply the event through the Go lifecycle."""

        actor = _text(actor_guid, "teammate actor_guid")
        if actor != self._active_actor:
            raise ResponsiveTeamBridgeAdapterV1Error(
                "requested actor is not the globally armed teammate"
            )
        armed = self._armed_by_actor.get(actor)
        if armed is None:
            raise ResponsiveTeamBridgeAdapterV1Error("teammate actor has no armed wake")
        synchronized_damage = self.sync_authoritative_damage_prefix()
        state = self.bridge.state()
        wake_state = _mapping(state.get("wake_ready"), "live wake state")
        expected_wake = {
            "schema": WAKE_SCHEMA,
            "model_content_sha256": self.model_provenance.model_content_sha256,
            "wake_id": armed["wake_id"],
            "time_ms": armed["time_ms"],
        }
        if dict(wake_state) != expected_wake or state.get("time_ms") != armed["time_ms"]:
            raise ResponsiveTeamBridgeAdapterV1Error(
                "simulator did not pause at the bound responsive teammate wake"
            )
        bridge_registry = self._validate_bridge_target_registry(state)

        sequence = armed["event_sequence"]
        tentative = deepcopy(self.runtime)
        tentative.advance_to(armed["time_ms"])
        model_input = tentative.snapshot_for_actor(actor)
        model_actor_identity = _model_actor_identity(tentative, actor)
        sampled = self.model.sample_emission(
            actor=model_actor_identity,
            emission_state=model_input,
            rng=random.Random(
                _substream_seed(self.branch.teammate_seed, actor, sequence, "emission")
            ),
        )
        event_type = _text(sampled.get("event_type"), "sampled event_type")
        if event_type not in _EVENT_TYPES:
            raise ResponsiveTeamBridgeAdapterV1Error("sampled event_type is unsupported")
        requested_damage = _number(sampled.get("sampled_damage"), "sampled damage")
        if event_type == "DMG" and requested_damage <= 0:
            raise ResponsiveTeamBridgeAdapterV1Error(
                "sampled DMG requires positive damage"
            )
        if event_type != "DMG" and requested_damage != 0:
            raise ResponsiveTeamBridgeAdapterV1Error(
                "sampled non-DMG event requires zero damage"
            )
        target_mode = _text(sampled.get("target_mode"), "sampled target_mode")
        eligible_target_guids = [
            self.target_guid_by_index[index]
            for index in bridge_registry["attackable_alive_target_indices"]
        ]
        selected_target_guid = _resolve_eligible_target(
            tentative,
            actor_guid=actor,
            target_mode=target_mode,
            eligible_target_guids=eligible_target_guids,
            rng=random.Random(
                _substream_seed(self.branch.teammate_seed, actor, sequence, "target")
            ),
        )
        expected_status = (
            "APPLIED" if event_type == "DMG" else "OBSERVED_NO_DAMAGE"
        )
        target_selection_basis = "ATTACKABLE_ALIVE_REGISTRY"
        if (
            event_type == "DMG"
            and selected_target_guid is None
            and not bridge_registry["attackable_alive_target_indices"]
            and bridge_registry["alive_target_indices"]
            and target_mode not in {"NO_TARGET", "NON_HOSTILE_OR_UNKNOWN"}
        ):
            # The proposal is due while every living target is temporarily
            # unattackable.  The Go protocol requires a positive DMG and a
            # concrete target to consume the ready wake, so choose a stable
            # living witness and let the authoritative lifecycle cancel it.
            # This proposal is not inserted into the learned event prefix.
            selected_target_guid = _resolve_eligible_target(
                tentative,
                actor_guid=actor,
                target_mode=target_mode,
                eligible_target_guids=[
                    self.target_guid_by_index[index]
                    for index in bridge_registry["alive_target_indices"]
                ],
                rng=random.Random(
                    _substream_seed(
                        self.branch.teammate_seed, actor, sequence, "target"
                    )
                ),
            )
            expected_status = "CANCELED_TARGET_UNATTACKABLE"
            target_selection_basis = "UNATTACKABLE_ALIVE_CANCEL_WITNESS"
        if event_type == "DMG" and selected_target_guid is None:
            raise ResponsiveTeamBridgeAdapterV1Error(
                "sampled teammate damage has no attackable living target"
            )
        if expected_status == "CANCELED_TARGET_UNATTACKABLE":
            transition = {
                "time_ms": armed["time_ms"],
                "actor_guid": actor,
                "actor_role": "TEAMMATE_RESPONSE_MODEL_CANCELED_PROPOSAL",
                "event_type": event_type,
                "target_guid": selected_target_guid,
                "requested_damage": requested_damage,
                "applied_damage": 0.0,
                "overkill_damage": requested_damage,
                "killed": False,
                "retargets": [],
                "all_targets_dead": tentative.kill_clock_ms is not None,
                "kill_clock_ms": tentative.kill_clock_ms,
                "observed_in_model_prefix": False,
                "cancellation_status": expected_status,
            }
        else:
            transition = tentative.apply_event(
                time_ms=armed["time_ms"],
                actor_guid=actor,
                event_type=event_type,
                spell_id=sampled.get("spell_id"),
                spell_name=sampled.get("spell_name"),
                attribution_kind=_text(
                    sampled.get("attribution_kind"), "sampled attribution_kind"
                ),
                exact_source_guid=sampled.get("exact_source_guid"),
                target_mode=(
                    target_mode if selected_target_guid is not None else "NO_TARGET"
                ),
                requested_damage=requested_damage,
                rng=random.Random(0),
                actor_role="TEAMMATE_RESPONSE_MODEL",
                explicit_target_guid=selected_target_guid,
            )
            transition["observed_in_model_prefix"] = True
        target_guid = transition["target_guid"]
        if target_guid != selected_target_guid:
            raise ResponsiveTeamBridgeAdapterV1Error(
                "runtime target differs from authoritative eligible selection"
            )
        target_index = (
            self.target_index_by_guid[target_guid] if target_guid is not None else None
        )
        event_id = f"event-{self.branch.content_sha256}-{actor}-{sequence}"
        event = {
            "schema": EVENT_SCHEMA,
            "model_content_sha256": self.model_provenance.model_content_sha256,
            "wake_id": armed["wake_id"],
            "event_id": event_id,
            "actor_guid": actor,
            "event_type": event_type,
            "target_index": target_index,
            "requested_damage": requested_damage,
        }
        response = self.bridge._request(EMIT_EVENT_COMMAND, responsive=event)
        receipt = _mapping(response.get("responsive_team_event"), "team event receipt")
        self._validate_event_receipt(
            receipt, event, transition, expected_status=expected_status
        )
        stream_receipt = self._read_just_emitted_receipt()
        if dict(stream_receipt) != dict(receipt):
            raise ResponsiveTeamBridgeAdapterV1Error(
                "immediate and cursor-stream teammate receipts differ"
            )
        self.runtime = tentative
        self._sequence_by_actor[actor] += 1
        del self._armed_by_actor[actor]
        del self._deadline_by_actor[actor]
        self._active_actor = None
        return {
            "schema": ADAPTER_SCHEMA,
            "status": "RESPONSIVE_TEAM_EVENT_ACCEPTED_BY_BRIDGE_CONTRACT",
            "branch_binding": self.branch.to_wire(),
            "branch_binding_sha256": self.branch.content_sha256,
            "model_provenance": self.model_provenance.to_wire(),
            "model_provenance_sha256": self.model_provenance.content_sha256,
            "model_input_live_prefix": model_input,
            "model_actor_identity": model_actor_identity,
            "authoritative_prefix_damage_receipts": list(synchronized_damage),
            "sampled_emission": dict(sampled),
            "wire_event": event,
            "wire_receipt": dict(receipt),
            "cursor_stream_receipt": dict(stream_receipt),
            "local_runtime_transition": transition,
            "target_selection_basis": target_selection_basis,
            "bridge_alive_target_indices_before_emission": list(
                bridge_registry["alive_target_indices"]
            ),
            "bridge_attackable_alive_target_indices_before_emission": list(
                bridge_registry["attackable_alive_target_indices"]
            ),
            "scheduler_order_key": [
                transition["time_ms"],
                actor,
                sequence,
            ],
            "candidate_policy_identity_visible_to_model": False,
            "future_candidate_suffix_visible_to_model": False,
            "realized_candidate_prefix_effects_visible_to_model": True,
            "development_only": True,
            "comparison_eligible": False,
            "voting_eligible": False,
            "deployment_eligible": False,
        }

    def emit_global_ready_and_rearm(self) -> JSONMap:
        """Emit the globally due event, retain other clocks, then arm the next."""

        actor = self._active_actor
        if actor is None:
            raise ResponsiveTeamBridgeAdapterV1Error(
                "no global responsive wake is active"
            )
        emitted = self.emit_ready(actor)
        next_wake = None
        discarded_after_kill_clock: list[JSONMap] = []
        if self.runtime.alive_target_guids():
            self._sample_actor_deadline(actor)
            next_wake = self.arm_global_next()
        else:
            discarded_after_kill_clock = [
                dict(row)
                for row in sorted(
                    self._deadline_by_actor.values(),
                    key=self._deadline_order_key,
                )
            ]
            self._deadline_by_actor.clear()
        return {
            "schema": f"{ADAPTER_SCHEMA}/scheduled_step",
            "status": "EMITTED_AND_NEXT_GLOBAL_WAKE_ARMED"
            if next_wake is not None
            else "EMITTED_TEAM_KILL_CLOCK_COMPLETE",
            "emitted": emitted,
            "next_wake": next_wake,
            "retained_actor_deadline_count": len(self._deadline_by_actor),
            "discarded_deadlines_after_team_kill_clock": discarded_after_kill_clock,
        }

    def _validate_bridge_target_registry(self, state: Mapping[str, Any]) -> JSONMap:
        """Bind target sampling to authoritative HP and attackability state."""

        background = _mapping(
            state.get("dynamic_team_background"),
            "live dynamic team background state",
        )
        rows = background.get("targets")
        if not isinstance(rows, list) or len(rows) != len(self.target_guid_by_index):
            raise ResponsiveTeamBridgeAdapterV1Error(
                "bridge live target registry differs from adapter registry"
            )
        alive: list[int] = []
        health_by_index: dict[int, float] = {}
        seen: set[int] = set()
        for position, value in enumerate(rows):
            row = _mapping(value, f"bridge target registry row {position}")
            target_index = _integer(
                row.get("target_index"),
                f"bridge target registry row {position} target_index",
            )
            if target_index >= len(self.target_guid_by_index) or target_index in seen:
                raise ResponsiveTeamBridgeAdapterV1Error(
                    "bridge live target registry has duplicate or out-of-range index"
                )
            seen.add(target_index)
            current_health = _number(
                row.get("current_health"),
                f"bridge target registry row {position} current_health",
            )
            dead = row.get("dead")
            if not isinstance(dead, bool) or dead != (current_health <= 0):
                raise ResponsiveTeamBridgeAdapterV1Error(
                    "bridge target dead flag differs from current health"
                )
            target_guid = self.target_guid_by_index[target_index]
            if current_health != float(self.runtime.health[target_guid]):
                raise ResponsiveTeamBridgeAdapterV1Error(
                    "bridge authoritative health differs from synchronized model prefix"
                )
            if not dead:
                alive.append(target_index)
            health_by_index[target_index] = current_health
        if seen != set(range(len(self.target_guid_by_index))):
            raise ResponsiveTeamBridgeAdapterV1Error(
                "bridge live target registry is incomplete"
            )
        semantics = _mapping(
            state.get("dynamic_target_semantics"),
            "live dynamic target semantics state",
        )
        semantic_rows = semantics.get("targets")
        if not isinstance(semantic_rows, list) or len(semantic_rows) != len(
            self.target_guid_by_index
        ):
            raise ResponsiveTeamBridgeAdapterV1Error(
                "bridge attackability registry differs from adapter registry"
            )
        attackable_alive: list[int] = []
        semantic_seen: set[int] = set()
        for position, value in enumerate(semantic_rows):
            row = _mapping(value, f"bridge target semantics row {position}")
            target_index = _integer(
                row.get("target_index"),
                f"bridge target semantics row {position} target_index",
            )
            if (
                target_index >= len(self.target_guid_by_index)
                or target_index in semantic_seen
            ):
                raise ResponsiveTeamBridgeAdapterV1Error(
                    "bridge attackability registry has duplicate or out-of-range index"
                )
            semantic_seen.add(target_index)
            current_health = _number(
                row.get("current_health"),
                f"bridge target semantics row {position} current_health",
            )
            dead = row.get("dead")
            attackable = row.get("attackable")
            if (
                current_health != health_by_index.get(target_index)
                or not isinstance(dead, bool)
                or dead != (current_health <= 0)
                or not isinstance(attackable, bool)
            ):
                raise ResponsiveTeamBridgeAdapterV1Error(
                    "bridge HP and attackability registries disagree"
                )
            if attackable and not dead:
                attackable_alive.append(target_index)
        if semantic_seen != set(range(len(self.target_guid_by_index))):
            raise ResponsiveTeamBridgeAdapterV1Error(
                "bridge attackability registry is incomplete"
            )
        return {
            "alive_target_indices": tuple(sorted(alive)),
            "attackable_alive_target_indices": tuple(sorted(attackable_alive)),
        }

    def _read_just_emitted_receipt(self) -> Mapping[str, Any]:
        response = self.bridge._request(
            EVENT_RECEIPTS_COMMAND, cursor=self.team_response_cursor
        )
        batch = _mapping(
            response.get("dynamic_team_response_receipts"),
            "dynamic team response receipt batch",
        )
        required = {
            "schema",
            "model_content_sha256",
            "environment_generation",
            "cursor",
            "next_cursor",
            "receipts",
        }
        if set(batch) != required:
            raise ResponsiveTeamBridgeAdapterV1Error(
                "dynamic team response receipt batch field set differs"
            )
        receipts = batch.get("receipts")
        if not isinstance(receipts, list) or len(receipts) != 1:
            raise ResponsiveTeamBridgeAdapterV1Error(
                "one newly emitted teammate event must yield one stream receipt"
            )
        if (
            batch.get("schema") != EVENT_RECEIPTS_SCHEMA
            or batch.get("model_content_sha256")
            != self.model_provenance.model_content_sha256
            or batch.get("environment_generation")
            != self.branch.environment_generation
            or batch.get("cursor") != self.team_response_cursor
            or batch.get("next_cursor") != self.team_response_cursor + 1
        ):
            raise ResponsiveTeamBridgeAdapterV1Error(
                "dynamic team response receipt batch binding differs"
            )
        self.team_response_cursor += 1
        return _mapping(receipts[0], "dynamic team response stream receipt")

    def _validate_event_receipt(
        self,
        receipt: Mapping[str, Any],
        event: Mapping[str, Any],
        transition: Mapping[str, Any],
        *,
        expected_status: str,
    ) -> None:
        required = {
            "schema",
            "model_content_sha256",
            "wake_id",
            "event_id",
            "time_ms",
            "actor_guid",
            "event_type",
            "target_index",
            "requested_damage",
            "applied_damage",
            "overkill_damage",
            "killed",
            "status",
            "damage_ordinal",
            "current_health",
            "all_targets_dead",
        }
        optional = {"retargeted_to"}
        if set(receipt) - optional != required or not set(receipt).issubset(
            required | optional
        ):
            raise ResponsiveTeamBridgeAdapterV1Error(
                "responsive team receipt field set differs"
            )
        status = _text(receipt.get("status"), "team event receipt status")
        scalar_expected = {
            "schema": EVENT_RECEIPT_SCHEMA,
            "model_content_sha256": self.model_provenance.model_content_sha256,
            "wake_id": event["wake_id"],
            "event_id": event["event_id"],
            "time_ms": transition["time_ms"],
            "actor_guid": event["actor_guid"],
            "event_type": event["event_type"],
            "target_index": event["target_index"],
        }
        if any(receipt.get(key) != value for key, value in scalar_expected.items()):
            raise ResponsiveTeamBridgeAdapterV1Error(
                "responsive team receipt identity differs from emitted event"
            )
        if status not in _EVENT_STATUSES or status != expected_status:
            raise ResponsiveTeamBridgeAdapterV1Error(
                "responsive team receipt status differs from the live target resolution"
            )
        for key, expected in (
            ("requested_damage", event["requested_damage"]),
            ("applied_damage", transition["applied_damage"]),
            ("overkill_damage", transition["overkill_damage"]),
        ):
            if _number(receipt.get(key), f"receipt {key}") != float(expected):
                raise ResponsiveTeamBridgeAdapterV1Error(
                    f"responsive team receipt {key} differs from runtime mirror"
                )
        if receipt.get("killed") is not transition["killed"]:
            raise ResponsiveTeamBridgeAdapterV1Error(
                "responsive team receipt killed flag differs from runtime mirror"
            )
        if receipt.get("all_targets_dead") is not transition["all_targets_dead"]:
            raise ResponsiveTeamBridgeAdapterV1Error(
                "responsive team receipt kill clock differs from runtime mirror"
            )
        target_guid = transition["target_guid"]
        expected_health = self.runtime.health.get(target_guid) if target_guid else None
        if target_guid is not None:
            expected_health = expected_health - transition["applied_damage"]
        if receipt.get("current_health") != expected_health:
            raise ResponsiveTeamBridgeAdapterV1Error(
                "responsive team receipt current health differs from runtime mirror"
            )
        ordinal = receipt.get("damage_ordinal")
        if event["event_type"] == "DMG":
            _integer(ordinal, "team event damage_ordinal", minimum=1)
        elif ordinal != 0:
            raise ResponsiveTeamBridgeAdapterV1Error(
                "non-damage team event must receive the zero damage ordinal"
            )
        if "retargeted_to" in receipt:
            _integer(receipt["retargeted_to"], "team event retargeted_to")


__all__ = [
    "ADAPTER_SCHEMA",
    "ARM_WAKE_COMMAND",
    "CausalBranchBindingV1",
    "EMIT_EVENT_COMMAND",
    "EVENT_RECEIPT_SCHEMA",
    "EVENT_RECEIPTS_COMMAND",
    "EVENT_RECEIPTS_SCHEMA",
    "EVENT_SCHEMA",
    "GAP_PROOF_SCHEMA",
    "LoadedResponsiveTeammateModelV1",
    "REQUIRED_COMMANDS",
    "ResponsiveTeamBridgeAdapterV1",
    "ResponsiveTeamBridgeAdapterV1Error",
    "SAME_TIMESTAMP_ORDER",
    "TeammateModelProvenanceV1",
    "WAKE_SCHEMA",
    "probe_responsive_team_protocol_gap_v1",
    "required_go_protocol_contract_v1",
    "validate_matched_causal_pair_v1",
]
