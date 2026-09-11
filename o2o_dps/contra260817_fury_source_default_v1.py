"""Bounded Contra260817 dual-wield Fury ``/contra c`` source adapter.

This module translates the readable ``Contra_new`` Lua source.  It does not
execute that Lua and it deliberately does not claim to reconstruct the user's
runtime profile.  Two immutable profile interpretations are provided:

``fresh_source_default``
    ``ContraDBDefault.Warrior.Buttons`` followed by the absent-field updates in
    ``Contra.OnLogin.InitWarriorMiniUI``.

``predicted_upgrade_preserve_current_buttons``
    A static prediction of ``InitDB`` applied to one content-addressed older
    SavedVariables snapshot.  The prediction is useful for development
    sensitivity only; it is not evidence that Contra260817 loaded or that this
    was the live profile used in play.

Only the level-60 dual-wield Fury A/B bodies selected by macro parameter ``c``
are in scope.  Raw sinks retain source order, including the source's unusual
OR precedence and same-invocation multiple spell attempts.  Missing dynamic
inputs fail closed before any sink is emitted.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import math
from typing import Any, Mapping

from .expert_policy import (
    ExpertDecision,
    ExpertProvenance,
    ExpertRole,
    ProvenanceKind,
    RawSink,
    StanceOp,
    SwingQueueOp,
    TargetOp,
    invalid_decision,
)
from .fury_contra_adapter_v2 import (
    ContraEvidenceKindV2,
    ContraFieldEvidenceV2,
    ContraTargetClassificationV2,
)
from .fury_expert_adapters import (
    BATTLE_SHOUT,
    BLOODRAGE,
    BLOODTHIRST,
    EXECUTE,
    HAMSTRING,
    WHIRLWIND,
    FuryExpertState,
    WeaponMode,
    _DecisionBuilder,
)


SOURCE_DEFAULT_POLICY_ID = "contra260817.fury.source_default_c.v1"
PREDICTED_UPGRADE_POLICY_ID = "contra260817.fury.predicted_upgrade_c.v1"
STATE_SCHEMA = "contra260817_fury_macro_c_state/v1"
PROFILE_SCHEMA = "contra260817_fury_profile_interpretation/v1"

OVERPOWER = "warrior.overpower"
BERSERKER_RAGE = "warrior.berserker_rage"
CONCUSSION_BLOW = "warrior.concussion_blow"
DEMORALIZING_SHOUT = "warrior.demoralizing_shout"

CONTRA260817_CODE_MANIFEST_SHA256 = (
    "91baa120a0c895f3a9b3f26d701eb6c48eda78c6f036ad9a04065c6b8990db6d"
)
FRESH_DEFAULT_DB_SHA256 = (
    "597062fcdb8673153397e94e2f8564eceaf1131bde8755165bd8a2759dee617b"
)
ONLOGIN_SHA256 = (
    "8ee56f9c584e4360242607bc08529b472653a515ca988e545a71e6e01d001430"
)
PREDICTED_SAVEDVARIABLES_SHA256 = (
    "27bc57d149aec789c4ff25505b8dd5c29b15d4cfecd96c37f37cbca2cc254bb2"
)

PACKAGE_BLOCKERS = (
    "missing_toc_member:Contra_Debuff.lua",
    "missing_toc_member:Contra_UI_DB.lua",
    "runtime_load_not_verified",
    "runtime_ContraDB_profile_unknown",
    "NameAndGuild_authorization_not_runtime_verified",
)


class Contra260817ProfileKindV1(str, Enum):
    """The only two audited profile interpretations accepted by this adapter."""

    FRESH_SOURCE_DEFAULT = "fresh_source_default"
    PREDICTED_UPGRADE_PRESERVE_CURRENT_BUTTONS = (
        "predicted_upgrade_preserve_current_buttons"
    )


@dataclass(frozen=True)
class Contra260817FuryProfileV1:
    """Fixed executable switches relevant to the bounded dual-wield policy."""

    kind: Contra260817ProfileKindV1
    profile_id: str
    expert_id: str
    source_version: int
    level: int
    mode: str
    autoselect: bool
    xuanfeng: bool
    burst: bool
    survive: bool
    interrupt: bool
    sunder: bool
    nuhou: bool
    yazhi: bool
    xuexing: bool
    kuangbao: bool
    zhendang: bool
    leiting: bool
    cuozhi: bool
    source_refs: tuple[str, ...]
    evidence_sha256: tuple[str, ...]
    profile_limit: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": PROFILE_SCHEMA,
            "kind": self.kind.value,
            "profile_id": self.profile_id,
            "expert_id": self.expert_id,
            "source_version": self.source_version,
            "level": self.level,
            "mode": self.mode,
            "buttons": {
                "autoselect": self.autoselect,
                "xuanfeng": self.xuanfeng,
                "Burst": self.burst,
                "Survive": self.survive,
                "interrupt": self.interrupt,
                "Sunder": self.sunder,
                "nuhou": self.nuhou,
                "yazhi": self.yazhi,
                "xuexing": self.xuexing,
                "kuangbao": self.kuangbao,
                "zhendang": self.zhendang,
                "leiting": self.leiting,
                "cuozhi": self.cuozhi,
            },
            "source_refs": list(self.source_refs),
            "evidence_sha256": list(self.evidence_sha256),
            "profile_limit": self.profile_limit,
            "source_execution": False,
            "runtime_profile_observed": False,
            "development_sensitivity_only": True,
            "evaluation_scope": "DEVELOPMENT_SENSITIVITY",
            "voting_status": "NONVOTING",
            "comparison_ready": False,
            "eligible_for_independent_vote": False,
        }


FRESH_SOURCE_DEFAULT_PROFILE = Contra260817FuryProfileV1(
    kind=Contra260817ProfileKindV1.FRESH_SOURCE_DEFAULT,
    profile_id="contra260817.fury.fresh_source_default.20251017022403",
    expert_id=SOURCE_DEFAULT_POLICY_ID,
    source_version=20251017022403,
    level=60,
    mode="副本模式",
    autoselect=True,
    xuanfeng=True,
    # Contra_DB defines legacy baofa/shengcun, but the executable helpers read
    # these absent uppercase fields; InitWarriorMiniUI therefore sets false.
    burst=False,
    survive=False,
    interrupt=False,
    sunder=False,
    nuhou=True,
    yazhi=True,
    xuexing=True,
    kuangbao=True,
    zhendang=True,
    leiting=True,
    cuozhi=True,
    source_refs=(
        "Contra_DB.lua:27492-27702",
        "Contra_Onlogin.lua:288-340",
    ),
    evidence_sha256=(FRESH_DEFAULT_DB_SHA256, ONLOGIN_SHA256),
    profile_limit=(
        "fresh source defaults only; not the user's current ContraDB profile"
    ),
)


PREDICTED_UPGRADE_PROFILE = Contra260817FuryProfileV1(
    kind=(
        Contra260817ProfileKindV1.PREDICTED_UPGRADE_PRESERVE_CURRENT_BUTTONS
    ),
    profile_id="contra260817.fury.predicted_upgrade.27bc57d1",
    expert_id=PREDICTED_UPGRADE_POLICY_ID,
    source_version=20251017022403,
    level=60,
    mode="副本模式",
    autoselect=False,
    xuanfeng=False,
    burst=False,
    survive=False,
    interrupt=False,
    sunder=False,
    nuhou=False,
    yazhi=False,
    xuexing=False,
    kuangbao=False,
    zhendang=False,
    leiting=False,
    cuozhi=False,
    source_refs=(
        "Contra_Onlogin.lua:288-340",
        "Contra.lua:2-160 (snapshot SHA-256 27bc57d1...)",
    ),
    evidence_sha256=(PREDICTED_SAVEDVARIABLES_SHA256, ONLOGIN_SHA256),
    profile_limit=(
        "static version-mismatch preservation prediction; no post-upgrade "
        "client load or logout observation"
    ),
)


_PROFILES: Mapping[
    Contra260817ProfileKindV1, Contra260817FuryProfileV1
] = {
    FRESH_SOURCE_DEFAULT_PROFILE.kind: FRESH_SOURCE_DEFAULT_PROFILE,
    PREDICTED_UPGRADE_PROFILE.kind: PREDICTED_UPGRADE_PROFILE,
}


@dataclass(frozen=True)
class Contra260817FuryMacroCStateV1:
    """Explicit inputs read by the bounded macro-c source traversal.

    The wrapper intentionally replaces convenient defaults such as
    ``FuryExpertState.target_is_boss`` and ``nearby_enemies``.  The Lua derives
    those facts from ``UnitClassification`` and a five-yard GUID/range count,
    so an absent value must not silently select an A/B branch.
    """

    combat: FuryExpertState
    authorization_gate_passed: bool | None
    attackable_units_within_five_yards: int | None
    current_target_within_five_yards: bool | None
    target_classification: ContraTargetClassificationV2 | str | None
    heroic_strike_actionbar_present: bool | None
    cleave_actionbar_present: bool | None
    combat_evidence: ContraFieldEvidenceV2 | None
    authorization_evidence: ContraFieldEvidenceV2 | None
    encounter_evidence: ContraFieldEvidenceV2 | None
    target_classification_evidence: ContraFieldEvidenceV2 | None
    actionbar_evidence: ContraFieldEvidenceV2 | None
    support_state_evidence: ContraFieldEvidenceV2 | None = None
    has_battle_shout: bool | None = None
    bloodrage_cooldown_remaining_s: float | None = None
    has_enrage_buff: bool | None = None
    fear_aura_active: bool | None = None
    has_demoralizing_shout_on_target: bool | None = None


class Contra260817FuryMacroCAdapterV1:
    """Source-order translator for a fixed dual-wield Fury macro-c profile."""

    def __init__(
        self,
        profile_kind: Contra260817ProfileKindV1 | str = (
            Contra260817ProfileKindV1.FRESH_SOURCE_DEFAULT
        ),
    ) -> None:
        try:
            normalized = Contra260817ProfileKindV1(profile_kind)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"unsupported Contra260817 profile: {profile_kind!r}") from exc
        self.profile = _PROFILES[normalized]
        self.expert_id = self.profile.expert_id

    def _provenance(self) -> ExpertProvenance:
        authority = [
            r"%BRAIN_OF_CAT_ROOT%\Contra_new\Contra.toc",
            r"%BRAIN_OF_CAT_ROOT%\Contra_new\Contra_DB.lua",
            r"%BRAIN_OF_CAT_ROOT%\Contra_new\Contra_Onlogin.lua",
            r"%BRAIN_OF_CAT_ROOT%\Contra_new\Contra_Macro.lua",
            r"%BRAIN_OF_CAT_ROOT%\Contra_new\Contra_Scrip_Warrior.lua",
        ]
        if self.profile.kind is (
            Contra260817ProfileKindV1.PREDICTED_UPGRADE_PRESERVE_CURRENT_BUTTONS
        ):
            authority.append(r"%WOW_CHARACTER_SAVEDVARIABLES%\Contra.lua")
        return ExpertProvenance(
            expert_id=self.expert_id,
            kind=ProvenanceKind.SOURCE_DERIVED,
            role=ExpertRole.CANDIDATE,
            authority_files=tuple(authority),
            source_refs=(
                "Contra_Macro.lua:221-292",
                "Contra_Scrip_Warrior.lua:134-193",
                "Contra_Scrip_Warrior.lua:253-325",
                "Contra_Scrip_Warrior.lua:404-467",
                "Contra_Scrip_Warrior.lua:580-583",
                "Contra_Scrip_Warrior.lua:1257-1315",
                *self.profile.source_refs,
            ),
        )

    def propose(self, state: Contra260817FuryMacroCStateV1) -> ExpertDecision:
        provenance = self._provenance()
        if not isinstance(state, Contra260817FuryMacroCStateV1):
            return invalid_decision(
                provenance,
                "Contra260817 macro-c requires its explicit v1 state wrapper",
                metadata=self._invalid_metadata(["v1_state_missing"]),
            )
        if not isinstance(state.combat, FuryExpertState):
            return invalid_decision(
                provenance,
                "Contra260817 macro-c combat state is invalid",
                metadata=self._invalid_metadata(["combat_state_invalid"]),
            )

        auth_errors = self._validate_authorization(state)
        if auth_errors:
            return invalid_decision(
                provenance,
                "Contra260817 NameAndGuild gate is unknown or invalid",
                metadata=self._invalid_metadata(auth_errors),
            )
        if state.authorization_gate_passed is False:
            builder = _DecisionBuilder()
            return builder.build(
                provenance,
                role_can_vote=False,
                reason="source NameAndGuild gate returned before macro-c dispatch",
                metadata=self._metadata(
                    state,
                    route="AUTHORIZATION_RETURN",
                    effective_nearby_count=None,
                    helper_no_sink_attempts=(),
                ),
            )

        errors = self._validate_reached_inputs(state)
        if errors:
            return invalid_decision(
                provenance,
                "Contra260817 macro-c reached inputs are unknown or invalid",
                metadata=self._invalid_metadata(errors),
            )

        combat = state.combat
        classification = ContraTargetClassificationV2(state.target_classification)
        effective = replace(
            combat,
            target_is_boss=(
                classification is ContraTargetClassificationV2.WORLDBOSS
            ),
            target_is_training_dummy=False,
        )
        count = int(state.attackable_units_within_five_yards)  # validated
        fallback_applied = (
            count == 0
            and effective.target_exists
            and state.current_target_within_five_yards is True
        )
        effective_count = 1 if fallback_applied else count
        route = "MULTI_B" if effective_count > 1 else "SINGLE_A"

        builder = _DecisionBuilder()
        helper_no_sink_attempts: list[dict[str, Any]] = []
        self._emit_attack_prelude(builder)
        self._emit_support(
            builder,
            state,
            effective,
            helper_no_sink_attempts=helper_no_sink_attempts,
        )
        builder.emit_stance(
            StanceOp.BERSERKER,
            operation="ContraZSCast",
            value="狂暴姿态",
            source_ref="Contra_Scrip_Warrior.lua:1263 or 1296",
        )

        if route == "MULTI_B":
            self._dual_wield_b(
                builder,
                effective,
                cleave_actionbar_present=bool(state.cleave_actionbar_present),
                helper_no_sink_attempts=helper_no_sink_attempts,
            )
        else:
            self._dual_wield_a(
                builder,
                effective,
                heroic_strike_actionbar_present=bool(
                    state.heroic_strike_actionbar_present
                ),
                helper_no_sink_attempts=helper_no_sink_attempts,
            )

        return builder.build(
            provenance,
            role_can_vote=False,
            reason=(
                "bounded bug-preserving Contra260817 dual-wield Fury macro-c "
                f"{route} source translation"
            ),
            metadata=self._metadata(
                state,
                route=route,
                effective_nearby_count=effective_count,
                helper_no_sink_attempts=helper_no_sink_attempts,
                count_fallback_applied=fallback_applied,
            ),
        )

    def _validate_authorization(
        self, state: Contra260817FuryMacroCStateV1
    ) -> list[str]:
        errors: list[str] = []
        if not isinstance(state.authorization_gate_passed, bool):
            errors.append("authorization_gate_unknown_or_invalid")
        self._validate_evidence(
            "authorization", state.authorization_evidence, errors
        )
        return errors

    def _validate_reached_inputs(
        self, state: Contra260817FuryMacroCStateV1
    ) -> list[str]:
        errors: list[str] = []
        combat = state.combat
        if combat.weapon_mode is not WeaponMode.DUAL_WIELD or combat.has_shield:
            errors.append("fixed_dual_wield_without_shield_required")
        if not combat.target_exists:
            errors.append("current_target_required_before_GetCombatInfo")
        if not isinstance(state.attackable_units_within_five_yards, int) or isinstance(
            state.attackable_units_within_five_yards, bool
        ) or (
            isinstance(state.attackable_units_within_five_yards, int)
            and state.attackable_units_within_five_yards < 0
        ):
            errors.append("five_yard_attackable_count_unknown_or_invalid")
        if not isinstance(state.current_target_within_five_yards, bool):
            errors.append("current_target_five_yard_state_unknown_or_invalid")
        try:
            ContraTargetClassificationV2(state.target_classification)
        except (TypeError, ValueError):
            errors.append("target_classification_unknown_or_invalid")
        for name, value in (
            ("heroic_strike_actionbar_present", state.heroic_strike_actionbar_present),
            ("cleave_actionbar_present", state.cleave_actionbar_present),
        ):
            if not isinstance(value, bool):
                errors.append(f"{name}_unknown_or_invalid")

        for name, evidence in (
            ("combat", state.combat_evidence),
            ("encounter", state.encounter_evidence),
            ("target_classification", state.target_classification_evidence),
            ("actionbar", state.actionbar_evidence),
        ):
            self._validate_evidence(name, evidence, errors)

        for name, value, minimum, maximum, strict_minimum in (
            ("rage", combat.rage, 0.0, 100.0, False),
            ("target_health_pct", combat.target_health_pct, 0.0, 100.0, False),
            ("bloodthirst_ready_in_s", combat.bloodthirst_ready_in_s, 0.0, None, False),
            ("whirlwind_ready_in_s", combat.whirlwind_ready_in_s, 0.0, None, False),
            ("contra_st_s", combat.contra_st_s, 0.0, None, False),
            ("contra_ss_s", combat.contra_ss_s, 0.0, None, False),
            ("contra_sd_s", combat.contra_sd_s, 0.0, None, True),
            ("target_distance_yards", combat.target_distance_yards, 0.0, None, False),
        ):
            if not _finite_in_range(value, minimum, maximum, strict_minimum):
                errors.append(f"{name}_unknown_or_invalid")

        if self._support_inputs_reached():
            self._validate_evidence(
                "support_state", state.support_state_evidence, errors
            )
            for name, value in (
                ("has_battle_shout", state.has_battle_shout),
                ("has_enrage_buff", state.has_enrage_buff),
                ("fear_aura_active", state.fear_aura_active),
                (
                    "has_demoralizing_shout_on_target",
                    state.has_demoralizing_shout_on_target,
                ),
            ):
                if not isinstance(value, bool):
                    errors.append(f"{name}_unknown_or_invalid")
            if not _finite_in_range(
                state.bloodrage_cooldown_remaining_s,
                0.0,
                None,
                False,
            ):
                errors.append("bloodrage_cooldown_remaining_s_unknown_or_invalid")
        return errors

    @staticmethod
    def _validate_evidence(
        name: str,
        evidence: ContraFieldEvidenceV2 | None,
        errors: list[str],
    ) -> None:
        if not isinstance(evidence, ContraFieldEvidenceV2) or (
            evidence.kind is ContraEvidenceKindV2.MISSING
        ):
            errors.append(f"{name}_evidence_missing_or_invalid")

    def _support_inputs_reached(self) -> bool:
        profile = self.profile
        return any(
            (
                profile.nuhou,
                profile.yazhi,
                profile.xuexing,
                profile.kuangbao,
                profile.zhendang,
                profile.leiting,
                profile.cuozhi,
            )
        )

    def _emit_attack_prelude(self, builder: _DecisionBuilder) -> None:
        if self.profile.autoselect:
            builder.target = TargetOp.AUTO_SWITCH
            builder.raw.append(
                RawSink(
                    "target",
                    "Contra.SelectNearestTarget",
                    "AUTO_SWITCH",
                    "Contra_Scrip_Warrior.lua:580-582;Contra_Lib.lua:51-105",
                )
            )
        builder.raw.append(
            RawSink(
                "autoattack",
                "Contra.StartAttack",
                "START",
                "Contra_Scrip_Warrior.lua:580-582;Contra_Lib.lua:10-48",
            )
        )

    def _emit_support(
        self,
        builder: _DecisionBuilder,
        state: Contra260817FuryMacroCStateV1,
        combat: FuryExpertState,
        *,
        helper_no_sink_attempts: list[dict[str, Any]],
    ) -> None:
        profile = self.profile
        if profile.nuhou and state.has_battle_shout is False:
            self._emit_gcd(builder, BATTLE_SHOUT, "战斗怒吼", 414)
        if (
            profile.yazhi
            and combat.target_health_pct > 21.0
            and 4.0 < combat.rage < 29.0
            and combat.in_melee_range
        ):
            self._emit_gcd(builder, OVERPOWER, "压制", 418)
        if (
            profile.xuexing
            and state.bloodrage_cooldown_remaining_s == 0.0
            and state.has_enrage_buff is False
            and combat.in_combat
            and combat.in_melee_range
        ):
            builder.emit_off_gcd(
                BLOODRAGE,
                operation="ContraZSCast",
                value="血性狂暴",
                source_ref="Contra_Scrip_Warrior.lua:422-424",
            )
        # ``IsTargetOfTargetMeor`` is an undefined global in the audited source.
        # For dual-wield Fury the remaining OR terms reduce this branch to fear.
        if profile.kuangbao and state.fear_aura_active and combat.in_combat:
            builder.emit_off_gcd(
                BERSERKER_RAGE,
                operation="ContraZSCast",
                value="狂暴之怒",
                source_ref="Contra_Scrip_Warrior.lua:426-438",
            )
        if profile.zhendang:
            # Dual-wield Bloodthirst and 21-point Concussion Blow cannot coexist
            # in a level-60 talent budget. Contra.CD returns 0 for an unknown
            # spell, so the source attempts the cast every invocation; the
            # client-side action is a statically known no-op.
            builder.emit_known_noop_gcd_attempt(
                CONCUSSION_BLOW,
                operation="CastSpellByName",
                value="震荡猛击",
                source_ref="Contra_Scrip_Warrior.lua:446-448",
                reason=(
                    "level60_dual_wield_bloodthirst_profile_cannot_know_"
                    "concussion_blow;Contra.CD_unknown_spell_returns_zero"
                ),
            )
        # The level<30 disjunct in the Thunder Clap branch is false for the
        # fixed level-60 profile.  Keep the source's always-true first OR intact;
        # it still does not make the full conjunction true.
        if (
            profile.cuozhi
            and state.has_demoralizing_shout_on_target is False
            and combat.in_melee_range
        ):
            self._emit_gcd(builder, DEMORALIZING_SHOUT, "挫志怒吼", 462)

    def _dual_wield_a(
        self,
        builder: _DecisionBuilder,
        state: FuryExpertState,
        *,
        heroic_strike_actionbar_present: bool,
        helper_no_sink_attempts: list[dict[str, Any]],
    ) -> None:
        hp = state.target_health_pct
        rage = state.rage
        is_boss = state.target_is_boss
        if (hp <= 20.0 and rage > 69.0 and is_boss) or (
            hp <= 20.0 and not is_boss
        ):
            self._emit_gcd(builder, EXECUTE, "斩杀", 1269)
        if (hp > 20.0 and rage > 59.0) or (
            hp > 21.0
            and state.bloodthirst_ready_in_s > state.contra_sd_s
            and rage > 47.0
        ):
            self._emit_next_swing(
                builder,
                state,
                SwingQueueOp.HEROIC_STRIKE,
                actionbar_present=heroic_strike_actionbar_present,
                source_ref="Contra_Scrip_Warrior.lua:1273-1275",
                helper_no_sink_attempts=helper_no_sink_attempts,
            )
        if (hp > 21.0 and state.contra_ss_s < 0.9) or (
            1.0 < hp < 19.0
            and 29.0 < rage < 45.0
            and is_boss
            and state.contra_ss_s < 0.5
        ):
            self._emit_gcd(builder, BLOODTHIRST, "嗜血", 1277)
        if (
            self.profile.xuanfeng
            and hp > 21.0
            and state.bloodthirst_ready_in_s > 1.5
            and state.contra_ss_s < 0.9
            and rage > 34.0
            and _whirlwind_range(state)
        ):
            self._emit_gcd(builder, WHIRLWIND, "旋风斩", 1281)
        if (
            hp > 21.0
            and state.bloodthirst_ready_in_s > 1.4
            and state.whirlwind_ready_in_s > 1.4
            and rage > 69.0
            and state.contra_ss_s < 0.9
            and not state.has_shield_break_trinket_buff
        ):
            self._emit_gcd(builder, HAMSTRING, "断筋", 1285)

    def _dual_wield_b(
        self,
        builder: _DecisionBuilder,
        state: FuryExpertState,
        *,
        cleave_actionbar_present: bool,
        helper_no_sink_attempts: list[dict[str, Any]],
    ) -> None:
        hp = state.target_health_pct
        rage = state.rage
        if (
            hp < 20.0
            and state.whirlwind_ready_in_s > state.contra_st_s
            and (
                (rage > 44.0 and state.contra_st_s > 0.5)
                or (
                    state.bloodthirst_ready_in_s != 0.0
                    and rage > 15.0
                    and state.contra_st_s > 0.5
                )
            )
        ):
            self._emit_gcd(builder, EXECUTE, "斩杀", 1300)

        # Preserve Lua precedence exactly:
        #   (xuanfeng and non-tauren and range<8) or (tauren and range<5.5)
        # The Tauren arm is intentionally not gated by xuanfeng.
        if (
            self.profile.xuanfeng
            and not state.race_is_tauren
            and state.target_distance_yards < 8.0
        ) or (
            state.race_is_tauren and state.target_distance_yards < 5.5
        ):
            self._emit_gcd(builder, WHIRLWIND, "旋风斩", 1304)
        if rage > 36.0 or (
            state.whirlwind_ready_in_s > state.contra_st_s and rage > 27.0
        ):
            self._emit_next_swing(
                builder,
                state,
                SwingQueueOp.CLEAVE,
                actionbar_present=cleave_actionbar_present,
                source_ref="Contra_Scrip_Warrior.lua:1308-1310",
                helper_no_sink_attempts=helper_no_sink_attempts,
            )
        if (
            hp > 20.0
            and rage > 68.0
            and state.whirlwind_ready_in_s > 1.4
        ) or (
            hp > 20.0
            and rage > 88.0
            and state.whirlwind_ready_in_s <= 1.4
        ) or (
            hp < 20.0
            and rage < 44.0
            and state.whirlwind_ready_in_s > 1.4
        ):
            self._emit_gcd(builder, BLOODTHIRST, "嗜血", 1312)

    @staticmethod
    def _emit_gcd(
        builder: _DecisionBuilder,
        action: str,
        localized_name: str,
        line: int,
    ) -> None:
        builder.emit_gcd(
            action,
            operation="CastSpellByName",
            value=localized_name,
            source_ref=f"Contra_Scrip_Warrior.lua:{line}",
        )

    @staticmethod
    def _emit_next_swing(
        builder: _DecisionBuilder,
        state: FuryExpertState,
        queue: SwingQueueOp,
        *,
        actionbar_present: bool,
        source_ref: str,
        helper_no_sink_attempts: list[dict[str, Any]],
    ) -> None:
        if state.queued_swing is queue:
            helper_no_sink_attempts.append(
                {
                    "operation": "Contra.IsHeroicStrikActive",
                    "requested_queue": queue.value,
                    "reason": "IsCurrentAction_returned_true",
                    "source_ref": source_ref,
                }
            )
            return
        if not actionbar_present:
            helper_no_sink_attempts.append(
                {
                    "operation": "Contra.IsHeroicStrikActive",
                    "requested_queue": queue.value,
                    "reason": "required_actionbar_texture_not_found",
                    "source_ref": source_ref,
                }
            )
            return
        localized = (
            "英勇打击" if queue is SwingQueueOp.HEROIC_STRIKE else "顺劈斩"
        )
        builder.emit_queue(
            queue,
            operation="CastSpellByName",
            value=localized,
            source_ref=source_ref,
        )

    def _invalid_metadata(self, errors: list[str]) -> dict[str, Any]:
        return {
            "state_schema": STATE_SCHEMA,
            "profile": self.profile.to_dict(),
            "fail_closed": True,
            "input_errors": list(dict.fromkeys(errors)),
            **self._fixed_boundary_metadata(),
        }

    def _metadata(
        self,
        state: Contra260817FuryMacroCStateV1,
        *,
        route: str,
        effective_nearby_count: int | None,
        helper_no_sink_attempts: Any,
        count_fallback_applied: bool = False,
    ) -> dict[str, Any]:
        evidence = {
            "combat": _evidence_dict(state.combat_evidence),
            "authorization": _evidence_dict(state.authorization_evidence),
            "encounter": _evidence_dict(state.encounter_evidence),
            "target_classification": _evidence_dict(
                state.target_classification_evidence
            ),
            "actionbar": _evidence_dict(state.actionbar_evidence),
            "support_state": _evidence_dict(state.support_state_evidence),
        }
        return {
            "state_schema": STATE_SCHEMA,
            "profile": self.profile.to_dict(),
            "macro_parameter": "c",
            "talent_label": "双持狂暴",
            "weapon_mode": WeaponMode.DUAL_WIELD.value,
            "route": route,
            "attackable_units_within_five_yards": (
                state.attackable_units_within_five_yards
            ),
            "effective_nearby_count": effective_nearby_count,
            "count_zero_target_fallback_applied": count_fallback_applied,
            "field_evidence": evidence,
            "helper_no_sink_attempts": list(helper_no_sink_attempts),
            "source_helper_returns": {
                "ZS_DaDuan": {
                    "value": "nil_falsey",
                    "reason": "profile_interrupt_false",
                    "continues_to_body": True,
                },
                "ZS_POJIA": {
                    "call_count": 1 if route == "MULTI_B" else 2,
                    "values": [
                        False for _ in range(1 if route == "MULTI_B" else 2)
                    ],
                    "reason": "profile_Sunder_false",
                    "continues_to_body": True,
                },
            },
            "source_execution": False,
            "runtime_profile_observed": False,
            "development_sensitivity_only": True,
            "evaluation_scope": "DEVELOPMENT_SENSITIVITY",
            "voting_status": "NONVOTING",
            "historical_truth": False,
            "comparison_ready": False,
            "independent_vote_allowed": False,
            "or_precedence_preserved": True,
            "multiple_sink_order_preserved": True,
            "source_return_semantics_preserved": True,
            **self._fixed_boundary_metadata(),
        }

    def _fixed_boundary_metadata(self) -> dict[str, Any]:
        blockers = list(PACKAGE_BLOCKERS)
        blockers.append(
            (
                "source_defaults_are_not_user_runtime_profile"
                if self.profile.kind
                is Contra260817ProfileKindV1.FRESH_SOURCE_DEFAULT
                else "upgrade_profile_is_prediction_not_post_load_observation"
            )
        )
        return {
            "source_code_manifest_sha256": CONTRA260817_CODE_MANIFEST_SHA256,
            "standalone_runnable": False,
            "ordered_executor_contract": "ExpertDecision.raw_sink_order",
            "ordered_executor_limit": (
                "target selection and fresh-default support actions still require "
                "explicit simulator operation coverage; existing executor must "
                "fail closed rather than drop them"
            ),
            "missing_toc_members": ["Contra_Debuff.lua", "Contra_UI_DB.lua"],
            "blockers": blockers,
        }


class Contra260817FurySourceDefaultMacroCAdapterV1(
    Contra260817FuryMacroCAdapterV1
):
    """Named entry point for the fresh-source-default sensitivity policy."""

    expert_id = SOURCE_DEFAULT_POLICY_ID

    def __init__(self) -> None:
        super().__init__(Contra260817ProfileKindV1.FRESH_SOURCE_DEFAULT)


class Contra260817FuryPredictedUpgradeMacroCAdapterV1(
    Contra260817FuryMacroCAdapterV1
):
    """Named entry point for the content-addressed upgrade prediction."""

    expert_id = PREDICTED_UPGRADE_POLICY_ID

    def __init__(self) -> None:
        super().__init__(
            Contra260817ProfileKindV1.PREDICTED_UPGRADE_PRESERVE_CURRENT_BUTTONS
        )


def _finite_in_range(
    value: Any,
    minimum: float,
    maximum: float | None,
    strict_minimum: bool,
) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    number = float(value)
    if not math.isfinite(number):
        return False
    if strict_minimum:
        if number <= minimum:
            return False
    elif number < minimum:
        return False
    return maximum is None or number <= maximum


def _whirlwind_range(state: FuryExpertState) -> bool:
    return (
        state.target_distance_yards < 5.5
        if state.race_is_tauren
        else state.target_distance_yards < 8.0
    )


def _evidence_dict(
    evidence: ContraFieldEvidenceV2 | None,
) -> dict[str, Any] | None:
    return evidence.to_dict() if isinstance(evidence, ContraFieldEvidenceV2) else None


__all__ = [
    "BERSERKER_RAGE",
    "CONCUSSION_BLOW",
    "CONTRA260817_CODE_MANIFEST_SHA256",
    "DEMORALIZING_SHOUT",
    "FRESH_SOURCE_DEFAULT_PROFILE",
    "OVERPOWER",
    "PACKAGE_BLOCKERS",
    "PREDICTED_SAVEDVARIABLES_SHA256",
    "PREDICTED_UPGRADE_POLICY_ID",
    "PREDICTED_UPGRADE_PROFILE",
    "PROFILE_SCHEMA",
    "SOURCE_DEFAULT_POLICY_ID",
    "STATE_SCHEMA",
    "Contra260817FuryMacroCAdapterV1",
    "Contra260817FuryMacroCStateV1",
    "Contra260817FuryPredictedUpgradeMacroCAdapterV1",
    "Contra260817FuryProfileV1",
    "Contra260817FurySourceDefaultMacroCAdapterV1",
    "Contra260817ProfileKindV1",
]
