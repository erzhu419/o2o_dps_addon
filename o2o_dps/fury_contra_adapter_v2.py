"""Versioned, fail-closed adapter for the deployed Contra Fury policy.

The original :mod:`o2o_dps.fury_expert_adapters` module is a frozen v1
artifact.  This module repairs one deliberately bounded omission without
changing that artifact: the TOC-loaded ``Contra_ALL.lua`` two-hand Raid-A
policy has three independent ``UnitHealthMax`` branches after its
boss/training-dummy body.

Contra computes ``IsBoss`` from ``UnitClassification("target") ==
"worldboss"`` and computes ``ZSSDW`` from six equipped Brotherhood set
pieces.  V2 therefore does not accept the convenient v1 defaults for either
fact.  Target fields and the complete equipped-item list are explicit,
provenanced inputs.  An unknown value fails closed before any action sink is
emitted.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import math
import re
from typing import Any

from .expert_policy import (
    ExpertDecision,
    ExpertProvenance,
    ExpertRole,
    ProvenanceKind,
    RawSink,
    StanceOp,
    invalid_decision,
)
from .fury_expert_adapters import (
    BLOODTHIRST,
    EXECUTE,
    SLAM,
    WHIRLWIND,
    ContraDeployedSourceAdapter,
    FuryExpertState,
    WeaponMode,
    _DecisionBuilder,
)


SOURCE_POLICY_ID = "contra.deployed.fury.raid_a"
SEMANTIC_ID = "contra.deployed.fury.raid_a.v2"
EVIDENCE_SCHEMA = "contra_field_evidence/v2"
TRAINING_DUMMY_NAME = "学徒训练假人"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_HYPOTHESIS_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")

BROTHERHOOD_SET_ITEMS = frozenset(
    {
        "兄弟会头盔",
        "兄弟会胸甲",
        "兄弟会护腿",
        "兄弟会肩甲",
        "兄弟会胫甲",
        "兄弟会项链",
    }
)


class ContraTargetClassificationV2(str, Enum):
    """Values returned by the vanilla/Turtle ``UnitClassification`` API."""

    WORLDBOSS = "worldboss"
    RARE_ELITE = "rareelite"
    ELITE = "elite"
    RARE = "rare"
    NORMAL = "normal"
    TRIVIAL = "trivial"
    MINUS = "minus"


class ContraEvidenceKindV2(str, Enum):
    """Evidence authority carried by one reconstructed Contra input.

    ``OBSERVED_SOURCE`` is reserved for a value present in a content-addressed
    source artifact. ``PINNED_STATIC_INPUT`` covers a fixed experiment input
    such as the equipped loadout. ``SIMULATOR_STATE`` covers dynamic state
    derived from a pinned simulator request or bridge. A sensitivity value is
    executable for branch analysis, but can never vote in a comparison.
    """

    OBSERVED_SOURCE = "OBSERVED_SOURCE"
    PINNED_STATIC_INPUT = "PINNED_STATIC_INPUT"
    SIMULATOR_STATE = "SIMULATOR_STATE"
    SENSITIVITY_HYPOTHESIS = "SENSITIVITY_HYPOTHESIS"
    MISSING = "MISSING"


@dataclass(frozen=True)
class ContraFieldEvidenceV2:
    """Typed, content-addressed evidence for one policy-relevant field."""

    kind: ContraEvidenceKindV2
    source_sha256: str | None = None
    corpus_sha256: str | None = None
    hypothesis_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ContraEvidenceKindV2):
            raise TypeError("evidence kind must be ContraEvidenceKindV2")
        for name, digest in (
            ("source_sha256", self.source_sha256),
            ("corpus_sha256", self.corpus_sha256),
        ):
            if digest is not None and (
                not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None
            ):
                raise ValueError(f"{name} must be a lowercase SHA-256 or None")

        if self.kind is ContraEvidenceKindV2.MISSING:
            if any(
                value is not None
                for value in (
                    self.source_sha256,
                    self.corpus_sha256,
                    self.hypothesis_id,
                )
            ):
                raise ValueError("MISSING evidence cannot carry a source or hypothesis")
            return

        if self.kind is ContraEvidenceKindV2.SENSITIVITY_HYPOTHESIS:
            if not isinstance(self.hypothesis_id, str) or (
                _HYPOTHESIS_ID_RE.fullmatch(self.hypothesis_id) is None
            ):
                raise ValueError(
                    "SENSITIVITY_HYPOTHESIS requires a normalized hypothesis_id"
                )
            return

        if self.hypothesis_id is not None:
            raise ValueError("non-sensitivity evidence cannot carry hypothesis_id")
        if self.source_sha256 is None and self.corpus_sha256 is None:
            raise ValueError(
                "observed, pinned, or simulator evidence requires a source/corpus SHA-256"
            )

    @property
    def sensitivity_only(self) -> bool:
        return self.kind is ContraEvidenceKindV2.SENSITIVITY_HYPOTHESIS

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": EVIDENCE_SCHEMA,
            "kind": self.kind.value,
            "source_sha256": self.source_sha256,
            "corpus_sha256": self.corpus_sha256,
            "hypothesis_id": self.hypothesis_id,
        }


@dataclass(frozen=True)
class ContraDeployedFuryStateV2:
    """All reconstructed inputs required by the deployed Contra adapter.

    ``combat`` supplies the common Fury timing/resource fields.  The target
    values below intentionally replace similarly named convenience fields in
    ``combat`` so a v1 default cannot silently choose a v2 source branch.
    Every policy-relevant value has a typed evidence object.  Observations,
    pinned inputs, and simulator state must bind to a content hash.  A value
    supplied only for sensitivity analysis must carry a hypothesis identity;
    it may execute but cannot become an independent baseline vote.  Missing
    local evidence remains explicit and fails closed.

    ``equipped_item_names`` is the complete equipped loadout, not a caller-
    supplied ZSSDW count.  The adapter reproduces ``Contra.HasEquipItem`` for
    each of the six source names and derives the count itself.
    """

    combat: FuryExpertState
    target_health_pct: float | None
    target_max_health: int | None
    target_classification: ContraTargetClassificationV2 | str | None
    target_name: str | None
    target_health_pct_evidence: ContraFieldEvidenceV2 | None
    target_max_health_evidence: ContraFieldEvidenceV2 | None
    target_classification_evidence: ContraFieldEvidenceV2 | None
    target_name_evidence: ContraFieldEvidenceV2 | None
    equipped_item_names: tuple[str, ...] | None
    equipment_evidence: ContraFieldEvidenceV2 | None
    target_position_evidence: ContraFieldEvidenceV2 | None


def derive_zssdw_from_loadout(equipped_item_names: tuple[str, ...]) -> int:
    """Reproduce the six boolean ``Contra.HasEquipItem`` increments."""

    equipped = set(equipped_item_names)
    return sum(name in equipped for name in BROTHERHOOD_SET_ITEMS)


class ContraDeployedFuryAdapterV2(ContraDeployedSourceAdapter):
    """Source-faithful deployed Contra Raid-A adapter, version 2.

    Raw sinks retain exact source order.  The shared decision builder keeps
    the final raw GCD sink as the normalized GCD lane, matching the existing
    adapter contract while preserving earlier same-call sinks for audit.
    """

    expert_id = SEMANTIC_ID

    def _provenance(self) -> ExpertProvenance:
        return ExpertProvenance(
            expert_id=self.expert_id,
            kind=ProvenanceKind.SOURCE_DERIVED,
            role=ExpertRole.DEPLOYED,
            authority_files=(
                r"%WOW_ADDONS_ROOT%\Contra\Contra.toc",
                r"%WOW_ADDONS_ROOT%\Contra\Contra_ALL.lua",
                r"%WOW_CHARACTER_SAVEDVARIABLES%\Contra.lua",
                r"%WOW_ROOT%\WTF\Config.wtf",
            ),
            source_refs=(
                "Contra_ALL.lua:30331-30337",
                "Contra_ALL.lua:31355-31387",
                "Contra_ALL.lua:31612-31732",
                "Contra_ALL.lua:36383-36431",
                "Contra_ALL.lua:36625-36631",
                "WTF/Config.wtf:112 (NP_QueueSpellsOnCooldown=0)",
            ),
        )

    def propose(self, state: ContraDeployedFuryStateV2) -> ExpertDecision:
        provenance = self._provenance()
        if not isinstance(state, ContraDeployedFuryStateV2):
            return invalid_decision(
                provenance,
                "v2 requires ContraDeployedFuryStateV2; v1 defaults are forbidden",
                metadata=self._invalid_metadata(["v2_state_missing"]),
            )
        if not isinstance(state.combat, FuryExpertState):
            return invalid_decision(
                provenance,
                "v2 combat state is unknown or invalid",
                metadata=self._invalid_metadata(["combat_state_invalid"]),
            )
        if not state.combat.target_exists:
            return invalid_decision(
                provenance,
                "current Contra autoselect=false has no target to evaluate",
                metadata=self._invalid_metadata(["target_missing"]),
            )

        errors, sensitivity_fields = self._validate_state(state)
        if errors:
            return invalid_decision(
                provenance,
                "Contra v2 required reconstructed inputs are unknown or invalid",
                metadata=self._invalid_metadata(errors),
            )

        # Validation proves these optionals are concrete and correctly typed.
        health_pct = float(state.target_health_pct)  # type: ignore[arg-type]
        max_health = int(state.target_max_health)  # type: ignore[arg-type]
        classification = ContraTargetClassificationV2(
            state.target_classification
        )
        target_name = str(state.target_name)
        equipped_item_names = state.equipped_item_names
        assert equipped_item_names is not None
        zssdw = derive_zssdw_from_loadout(equipped_item_names)
        is_boss = classification is ContraTargetClassificationV2.WORLDBOSS
        is_training_dummy = target_name == TRAINING_DUMMY_NAME

        effective = replace(
            state.combat,
            target_health_pct=health_pct,
            target_name=target_name,
            target_is_boss=is_boss,
            target_is_training_dummy=is_training_dummy,
            contra_zssdw=zssdw,
        )
        metadata = self._metadata_v2(
            state,
            classification=classification,
            zssdw=zssdw,
            is_boss=is_boss,
            is_training_dummy=is_training_dummy,
            sensitivity_fields=sensitivity_fields,
        )
        role_can_vote = not sensitivity_fields

        builder = _DecisionBuilder()
        builder.raw.append(
            RawSink(
                "autoattack",
                "Contra.StartAttack",
                "START",
                self._ref("prelude"),
            )
        )
        if effective.contra_interrupt_action:
            self._emit_contra_gcd(
                builder,
                effective,
                effective.contra_interrupt_action,
                self._ref("interrupt"),
            )
            return builder.build(
                provenance,
                role_can_vote=role_can_vote,
                reason="Contra interrupt prelude returned before the Fury body",
                metadata=metadata,
            )
        for action in effective.contra_prelude_off_gcd:
            builder.emit_off_gcd(
                action,
                operation="Contra prelude helper",
                value=action,
                source_ref=self._ref("prelude"),
            )
        if not effective.has_shield:
            builder.emit_stance(
                StanceOp.BERSERKER,
                operation="ContraZSCast",
                value="狂暴姿态",
                source_ref=self._ref("stance"),
            )

        if effective.weapon_mode is WeaponMode.TWO_HAND:
            self._two_hand_complete(
                builder,
                effective,
                max_health=max_health,
                is_boss=is_boss,
                is_training_dummy=is_training_dummy,
            )
        else:
            self._dual_wield(builder, effective)

        return builder.build(
            provenance,
            role_can_vote=role_can_vote,
            reason=(
                "versioned bug-preserving translation of TOC-loaded "
                "Contra_ALL.lua Raid-A policy"
            ),
            metadata=metadata,
        )

    def _validate_state(
        self, state: ContraDeployedFuryStateV2
    ) -> tuple[list[str], tuple[str, ...]]:
        errors: list[str] = []
        sensitivity_fields: list[str] = []
        pct = state.target_health_pct
        if (
            isinstance(pct, bool)
            or not isinstance(pct, (int, float))
            or not math.isfinite(float(pct))
            or not 0.0 <= float(pct) <= 100.0
        ):
            errors.append("target_health_pct_unknown_or_invalid")
        self._validate_field_evidence(
            "target_health_pct",
            state.target_health_pct_evidence,
            errors,
            sensitivity_fields,
        )

        max_health = state.target_max_health
        if (
            isinstance(max_health, bool)
            or not isinstance(max_health, int)
            or max_health <= 0
        ):
            errors.append("target_max_health_unknown_or_invalid")
        self._validate_field_evidence(
            "target_max_health",
            state.target_max_health_evidence,
            errors,
            sensitivity_fields,
        )

        try:
            ContraTargetClassificationV2(state.target_classification)
        except (TypeError, ValueError):
            errors.append("target_classification_unknown_or_invalid")
        self._validate_field_evidence(
            "target_classification",
            state.target_classification_evidence,
            errors,
            sensitivity_fields,
        )

        if not isinstance(state.target_name, str) or not state.target_name.strip():
            errors.append("target_name_unknown_or_invalid")
        self._validate_field_evidence(
            "target_name",
            state.target_name_evidence,
            errors,
            sensitivity_fields,
        )

        names = state.equipped_item_names
        if not isinstance(names, tuple) or any(
            not isinstance(name, str) or not name.strip() for name in names
        ):
            errors.append("equipped_item_names_unknown_or_invalid")
        self._validate_field_evidence(
            "equipped_item_names",
            state.equipment_evidence,
            errors,
            sensitivity_fields,
        )

        target_distance = state.combat.target_distance_yards
        if (
            isinstance(target_distance, bool)
            or not isinstance(target_distance, (int, float))
            or not math.isfinite(float(target_distance))
            or float(target_distance) < 0.0
        ):
            errors.append("target_position_unknown_or_invalid")
        self._validate_field_evidence(
            "target_position",
            state.target_position_evidence,
            errors,
            sensitivity_fields,
        )
        return errors, tuple(sensitivity_fields)

    @staticmethod
    def _validate_field_evidence(
        field_name: str,
        evidence: ContraFieldEvidenceV2 | None,
        errors: list[str],
        sensitivity_fields: list[str],
    ) -> None:
        # Runtime checking is deliberate: Python type annotations alone would
        # allow a legacy/fabricated non-empty provenance string to pass in.
        if not isinstance(evidence, ContraFieldEvidenceV2):
            errors.append(f"{field_name}_evidence_missing_or_invalid")
            return
        if evidence.kind is ContraEvidenceKindV2.MISSING:
            errors.append(f"{field_name}_evidence_missing")
            return
        if evidence.sensitivity_only:
            sensitivity_fields.append(field_name)

    def _invalid_metadata(self, errors: list[str]) -> dict[str, Any]:
        return {
            "adapter_contract": "fury_contra_adapter/v2",
            "evidence_contract": EVIDENCE_SCHEMA,
            "semantic_parent_id": SOURCE_POLICY_ID,
            "toc_loaded_file": "Contra_ALL.lua",
            "fail_closed": True,
            "input_errors": list(errors),
            "independent_vote_allowed": False,
        }

    def _metadata_v2(
        self,
        state: ContraDeployedFuryStateV2,
        *,
        classification: ContraTargetClassificationV2,
        zssdw: int,
        is_boss: bool,
        is_training_dummy: bool,
        sensitivity_fields: tuple[str, ...],
    ) -> dict[str, Any]:
        metadata = super()._metadata(state.combat)
        metadata.update(
            {
                "adapter_contract": "fury_contra_adapter/v2",
                "evidence_contract": EVIDENCE_SCHEMA,
                "semantic_parent_id": SOURCE_POLICY_ID,
                "complete_two_hand_raid_a_body": True,
                "target_health_pct": float(state.target_health_pct),
                "target_max_health": int(state.target_max_health),
                "target_classification": classification.value,
                "target_name": state.target_name,
                "source_is_boss": is_boss,
                "source_is_training_dummy": is_training_dummy,
                "derived_zssdw": zssdw,
                "zssdw_derivation": (
                    "count_presence_of_six_Contra_ALL.lua_Brotherhood_items"
                ),
                "field_evidence": {
                    "target_health_pct": state.target_health_pct_evidence.to_dict(),
                    "target_max_health": state.target_max_health_evidence.to_dict(),
                    "target_classification": (
                        state.target_classification_evidence.to_dict()
                    ),
                    "target_name": state.target_name_evidence.to_dict(),
                    "equipped_item_names": state.equipment_evidence.to_dict(),
                    "target_position": state.target_position_evidence.to_dict(),
                },
                "sensitivity_only": bool(sensitivity_fields),
                "sensitivity_fields": list(sensitivity_fields),
                "independent_vote_allowed": not sensitivity_fields,
                "legacy_combat_target_flags_ignored": True,
                "max_health_branches_are_independent_source_ifs": True,
            }
        )
        return metadata

    def _two_hand_complete(
        self,
        builder: _DecisionBuilder,
        state: FuryExpertState,
        *,
        max_health: int,
        is_boss: bool,
        is_training_dummy: bool,
    ) -> None:
        # These are four independent source `if` statements.  In particular,
        # the medium and low max-health blocks are not guarded by `not IsBoss`,
        # so a small worldboss/dummy runs both its first body and one size body.
        if is_boss or is_training_dummy:
            self._two_hand_raid_target(builder, state)
        if max_health >= 51000 and not is_boss and not is_training_dummy:
            self._two_hand_large_nonboss(builder, state)
        if 25000 <= max_health < 51000:
            self._two_hand_medium(builder, state, is_boss=is_boss)
        if max_health < 25000:
            self._two_hand_small(builder, state)

    def _two_hand_raid_target(
        self, builder: _DecisionBuilder, state: FuryExpertState
    ) -> None:
        in_ww_range = state.contra_whirlwind_range()
        if not state.flurry_active and state.target_health_pct > 70.0:
            # Lua `and` binds more tightly than `or`.  The source omitted
            # parentheses around this first range check, so a Tauren in range
            # enters even when saved xuanfeng=false.  Keep that source bug and
            # its sink position before the following Bloodthirst call.
            initial_ww = (
                self.xuanfeng
                and not state.race_is_tauren
                and state.target_distance_yards < 8.0
            ) or (
                state.race_is_tauren and state.target_distance_yards < 5.5
            )
            if initial_ww:
                self._emit_contra_gcd(
                    builder, state, WHIRLWIND, self._two_ref(31618)
                )
            if not self.xuanfeng and state.bloodthirst_ready_in_s == 0.0:
                self._emit_contra_gcd(
                    builder, state, BLOODTHIRST, self._two_ref(31621)
                )

        # The three ~= terms are joined with OR in the deployed source.  The
        # expression is consequently true for every LastCastName.
        if state.target_health_pct > 20.0:
            if self.xuanfeng and in_ww_range:
                self._emit_contra_gcd(
                    builder, state, WHIRLWIND, self._two_ref(31625)
                )
            if not self.xuanfeng:
                self._emit_contra_gcd(
                    builder, state, BLOODTHIRST, self._two_ref(31628)
                )

        self._emit_two_hand_queue(
            builder,
            state,
            high_swing_threshold=82.0,
            low_swing_threshold=67.0,
            low_set_high_swing_threshold=87.0,
            low_set_low_swing_threshold=72.0,
            high_set_ref=31631,
            low_set_ref=31634,
        )

        if state.target_health_pct > 20.0:
            if self.xuanfeng:
                slam = state.slam_remaining_s == 0.0 and (
                    state.last_cast_name in {BLOODTHIRST, WHIRLWIND}
                    or (
                        state.bloodthirst_ready_in_s != 0.0
                        and state.whirlwind_ready_in_s != 0.0
                        and state.contra_st_s > 1.2
                    )
                )
            else:
                slam = state.last_cast_name == BLOODTHIRST or (
                    state.bloodthirst_ready_in_s != 0.0
                    and state.contra_st_s > 1.2
                )
            if slam:
                self._emit_contra_gcd(
                    builder,
                    state,
                    SLAM,
                    self._two_ref(31638 if self.xuanfeng else 31641),
                )
            if (
                state.last_cast_name == SLAM
                and state.bloodthirst_ready_in_s < 0.5
            ):
                self._emit_contra_gcd(
                    builder, state, BLOODTHIRST, self._two_ref(31644)
                )
            if (
                self.xuanfeng
                and state.last_cast_name == SLAM
                and state.whirlwind_ready_in_s < 0.5
                and in_ww_range
                and state.rage > 34.0
            ):
                self._emit_contra_gcd(
                    builder, state, WHIRLWIND, self._two_ref(31647)
                )

        if 2.0 < state.target_health_pct <= 20.0:
            if (
                state.contra_ss_s < 1.2
                and state.bloodthirst_ready_in_s == 0.0
                and state.rage > 44.0
            ):
                self._emit_contra_gcd(
                    builder, state, BLOODTHIRST, self._two_ref(31652)
                )
            if state.contra_ss_s > 1.0 or (
                state.rage > 94.0 and state.bloodthirst_ready_in_s != 0.0
            ):
                self._emit_contra_gcd(
                    builder, state, EXECUTE, self._two_ref(31653)
                )
            if (
                state.contra_st_s > state.slam_cast_time_s + 0.3
                and state.bloodthirst_ready_in_s != 0.0
            ) or (
                state.rage <= 44.0
                and state.contra_st_s > state.slam_cast_time_s + 0.3
                and state.bloodthirst_ready_in_s == 0.0
            ):
                self._emit_contra_gcd(
                    builder, state, SLAM, self._two_ref(31654)
                )
        if state.target_health_pct <= 2.0:
            if state.slam_remaining_s > 0.2:
                builder.emit_stop_cast(source_ref=self._two_ref(31656))
            self._emit_contra_gcd(builder, state, EXECUTE, self._two_ref(31659))

    def _two_hand_large_nonboss(
        self, builder: _DecisionBuilder, state: FuryExpertState
    ) -> None:
        if state.target_health_pct > 20.0:
            self._emit_two_hand_queue(
                builder,
                state,
                high_swing_threshold=82.0,
                low_swing_threshold=67.0,
                low_set_high_swing_threshold=87.0,
                low_set_low_swing_threshold=72.0,
                high_set_ref=31665,
                low_set_ref=31668,
            )
            if self.xuanfeng and state.whirlwind_ready_in_s < 6.5:
                self._emit_contra_gcd(
                    builder, state, BLOODTHIRST, self._two_ref(31671)
                )
            if not self.xuanfeng:
                self._emit_contra_gcd(
                    builder, state, BLOODTHIRST, self._two_ref(31672)
                )
            if (
                self.xuanfeng
                and state.bloodthirst_ready_in_s < 4.5
                and state.contra_whirlwind_range()
                and state.rage > 34.0
            ):
                self._emit_contra_gcd(
                    builder, state, WHIRLWIND, self._two_ref(31673)
                )
            if self.xuanfeng:
                slam = state.slam_remaining_s == 0.0 and (
                    state.last_cast_name in {BLOODTHIRST, WHIRLWIND}
                    or (
                        state.bloodthirst_ready_in_s != 0.0
                        and state.whirlwind_ready_in_s != 0.0
                        and state.contra_st_s > 1.2
                    )
                )
            else:
                slam = state.last_cast_name == BLOODTHIRST or (
                    state.bloodthirst_ready_in_s != 0.0
                    and state.contra_st_s > 1.2
                )
            if slam:
                self._emit_contra_gcd(
                    builder, state, SLAM, self._two_ref(31674 if self.xuanfeng else 31677)
                )
        if state.target_health_pct <= 20.0:
            if state.slam_remaining_s > 0.2:
                builder.emit_stop_cast(source_ref=self._two_ref(31682))
            if state.rage < 35.0:
                self._emit_contra_gcd(
                    builder, state, BLOODTHIRST, self._two_ref(31683)
                )
            if state.rage >= 35.0 or state.bloodthirst_ready_in_s != 0.0:
                self._emit_contra_gcd(
                    builder, state, EXECUTE, self._two_ref(31684)
                )

    def _two_hand_medium(
        self,
        builder: _DecisionBuilder,
        state: FuryExpertState,
        *,
        is_boss: bool,
    ) -> None:
        if state.target_health_pct < 20.0 and state.slam_remaining_s > 0.2:
            builder.emit_stop_cast(source_ref=self._two_ref(31688))
        self._emit_two_hand_queue(
            builder,
            state,
            high_swing_threshold=82.0,
            low_swing_threshold=67.0,
            low_set_high_swing_threshold=87.0,
            low_set_low_swing_threshold=72.0,
            high_set_ref=31691,
            low_set_ref=31694,
        )
        bloodthirst = (
            state.target_health_pct > 21.0
            and state.contra_st_s > 0.5
            and state.bloodthirst_ready_in_s == 0.0
        ) or (
            state.target_health_pct < 19.0
            and state.contra_st_s > 1.5
            and state.bloodthirst_ready_in_s == 0.0
            and is_boss
            and (state.rage > 44.0 or state.rage < 35.0)
        )
        if bloodthirst:
            self._emit_contra_gcd(
                builder, state, BLOODTHIRST, self._two_ref(31697)
            )
        if state.target_health_pct < 20.0:
            self._emit_contra_gcd(builder, state, EXECUTE, self._two_ref(31700))
        if (
            self.xuanfeng
            and state.target_health_pct > 21.0
            and state.bloodthirst_ready_in_s > 1.0
            and state.whirlwind_ready_in_s == 0.0
            and state.contra_whirlwind_range()
            and state.rage > 39.0
            and (state.contra_st_s < 1.8 or state.contra_ss_s < 0.2)
        ):
            self._emit_contra_gcd(
                builder, state, WHIRLWIND, self._two_ref(31703)
            )
        if (
            state.target_health_pct > 21.0
            and state.contra_st_s > state.slam_cast_time_s
            and state.bloodthirst_ready_in_s > 0.1
        ):
            self._emit_contra_gcd(builder, state, SLAM, self._two_ref(31706))

    def _two_hand_small(
        self, builder: _DecisionBuilder, state: FuryExpertState
    ) -> None:
        if state.target_health_pct < 20.0 and state.slam_remaining_s > 0.2:
            builder.emit_stop_cast(source_ref=self._two_ref(31711))
        self._emit_two_hand_queue(
            builder,
            state,
            high_swing_threshold=52.0,
            low_swing_threshold=34.0,
            low_set_high_swing_threshold=57.0,
            low_set_low_swing_threshold=39.0,
            high_set_ref=31714,
            low_set_ref=31717,
        )
        if (
            state.target_health_pct > 21.0
            and state.bloodthirst_ready_in_s == 0.0
        ):
            self._emit_contra_gcd(
                builder, state, BLOODTHIRST, self._two_ref(31720)
            )
        if state.target_health_pct < 20.0:
            self._emit_contra_gcd(builder, state, EXECUTE, self._two_ref(31723))
        if (
            self.xuanfeng
            and state.target_health_pct > 21.0
            and state.bloodthirst_ready_in_s > 1.0
            and state.whirlwind_ready_in_s == 0.0
            and state.contra_whirlwind_range()
            and state.rage > 39.0
            and (state.contra_st_s < 1.8 or state.contra_ss_s < 0.2)
        ):
            self._emit_contra_gcd(
                builder, state, WHIRLWIND, self._two_ref(31726)
            )
        if (
            state.target_health_pct > 21.0
            and state.contra_st_s > state.slam_cast_time_s
            and state.bloodthirst_ready_in_s > 1.5
            and state.whirlwind_ready_in_s > 1.5
        ):
            self._emit_contra_gcd(builder, state, SLAM, self._two_ref(31729))

    def _emit_two_hand_queue(
        self,
        builder: _DecisionBuilder,
        state: FuryExpertState,
        *,
        high_swing_threshold: float,
        low_swing_threshold: float,
        low_set_high_swing_threshold: float,
        low_set_low_swing_threshold: float,
        high_set_ref: int,
        low_set_ref: int,
    ) -> None:
        if state.contra_zssdw >= 3:
            should_queue = state.target_health_pct > 20.0 and (
                (state.rage > high_swing_threshold and state.contra_st_s > 1.0)
                or (state.rage > low_swing_threshold and state.contra_st_s < 1.0)
            )
            source_ref = high_set_ref
        else:
            should_queue = state.target_health_pct > 20.0 and (
                (
                    state.rage > low_set_high_swing_threshold
                    and state.contra_st_s > 1.0
                )
                or (
                    state.rage > low_set_low_swing_threshold
                    and state.contra_st_s < 1.0
                )
            )
            source_ref = low_set_ref
        if should_queue:
            self._emit_heroic_strike(builder, state, self._two_ref(source_ref))


__all__ = [
    "BROTHERHOOD_SET_ITEMS",
    "EVIDENCE_SCHEMA",
    "SOURCE_POLICY_ID",
    "SEMANTIC_ID",
    "ContraDeployedFuryAdapterV2",
    "ContraDeployedFuryStateV2",
    "ContraEvidenceKindV2",
    "ContraFieldEvidenceV2",
    "ContraTargetClassificationV2",
    "derive_zssdw_from_loadout",
]
