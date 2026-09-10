"""Bounded Fury Warrior adapters for Cat, Cat2, Contra, and contra_new.

These adapters translate readable policy source into deterministic proposals;
they do not execute the addons' Lua.  The installed Cat and Contra sources are
eligible proposal families, while contra_new and an explicitly curated Cat2
card stack remain non-voting research candidates.  Cat2 without a saved
configuration returns an explicit invalid result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

from .expert_policy import (
    WAIT_ACTION,
    CastControl,
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


BLOODTHIRST = "warrior.bloodthirst"
WHIRLWIND = "warrior.whirlwind"
EXECUTE = "warrior.execute"
SLAM = "warrior.slam"
HAMSTRING = "warrior.hamstring"
PUMMEL = "warrior.pummel"
BATTLE_SHOUT = "warrior.battle_shout"
BLOODRAGE = "warrior.bloodrage"
DEATH_WISH = "warrior.death_wish"
SUNDER_ARMOR = "warrior.sunder_armor"


class WeaponMode(str, Enum):
    TWO_HAND = "TWO_HAND"
    DUAL_WIELD = "DUAL_WIELD"


@dataclass(frozen=True)
class FuryExpertState:
    """Normalized state required by the bounded source translators.

    ``contra_st_s``, ``contra_ss_s``, and ``contra_sd_s`` intentionally retain
    the deployed source's native names.  Their exact game-side derivation must
    be supplied by state reconstruction rather than guessed in this adapter.
    """

    rage: float
    target_health_pct: float
    weapon_mode: WeaponMode = WeaponMode.DUAL_WIELD
    target_exists: bool = True
    target_is_boss: bool = True
    target_is_training_dummy: bool = False
    target_name: str = ""
    target_level: int | None = None
    in_combat: bool = True
    in_melee_range: bool = True
    target_distance_yards: float = 3.0
    nearby_enemies: int = 1
    gcd_ready: bool = True
    current_stance: StanceOp = StanceOp.BERSERKER
    has_shield: bool = False
    has_battle_shout: bool = True
    battle_shout_remaining_s: float = 600.0
    flurry_talent: bool = True
    flurry_active: bool = True
    bloodthirst_known: bool = True
    bloodthirst_ready_in_s: float = 0.0
    whirlwind_ready_in_s: float = 0.0
    bloodrage_ready: bool = False
    death_wish_ready: bool = False
    mainhand_swing_remaining_s: float = 2.5
    mainhand_swing_duration_s: float = 3.5
    execute_cost: float = 15.0
    heroic_strike_cost: float = 15.0
    cleave_cost: float = 20.0
    whirlwind_cost: float = 25.0
    slam_cast_time_s: float = 1.5
    slam_remaining_s: float = 0.0
    casting_slam: bool = False
    last_cast_name: str = ""
    queued_swing: SwingQueueOp = SwingQueueOp.KEEP
    race_is_tauren: bool = False
    contra_st_s: float = 0.0
    contra_ss_s: float = 0.0
    contra_sd_s: float = 0.0
    contra_zssdw: int = 3
    contra_interrupt_action: str | None = None
    contra_prelude_off_gcd: tuple[str, ...] = ()
    has_shield_break_trinket_buff: bool = False
    sunder_action_required: bool = False
    nampower: bool = True

    def cooldown_ready(self, ready_in_s: float) -> bool:
        return ready_in_s <= 0.0

    def cooldown_within(self, ready_in_s: float, seconds: float) -> bool:
        return ready_in_s <= seconds

    def contra_whirlwind_range(self) -> bool:
        limit = 5.5 if self.race_is_tauren else 8.0
        return self.target_distance_yards < limit


@dataclass(frozen=True)
class CatFuryProfile1:
    """The currently selected Cat Fury SavedVariables profile."""

    profile_index: int = 1
    heroic_strike_mode: str = "DYNAMIC"
    heroic_strike_fixed_threshold: float = 50.0
    whirlwind_enabled: bool = True
    execute_enabled: bool = True
    execute_non_boss: bool = True
    nearby_enemy_switch_enabled: bool = True
    slam_timing_s: float = 1.500000014901161
    hamstring_enabled: bool = True
    battle_shout_enabled: bool = True
    berserker_stance_required: bool = True
    bloodrage_enabled: bool = True
    target_mode: int = 0


CAT_FURY_PROFILE1 = CatFuryProfile1()


@dataclass
class _DecisionBuilder:
    gcd: str = WAIT_ACTION
    wait_ms: int | None = 100
    queue: SwingQueueOp = SwingQueueOp.KEEP
    off_gcd: list[str] = field(default_factory=list)
    stance: StanceOp = StanceOp.KEEP
    target: TargetOp = TargetOp.KEEP
    cast_control: CastControl = CastControl.KEEP
    raw: list[RawSink] = field(default_factory=list)
    gcd_calls: list[str] = field(default_factory=list)
    known_noop_gcd_attempts: list[dict[str, str]] = field(default_factory=list)

    def emit_gcd(
        self,
        action: str,
        *,
        operation: str,
        value: str,
        source_ref: str,
    ) -> None:
        # Contra may emit several GCD sinks in one macro call.  The normalized
        # lane records the final source sink while raw keeps every call.
        self.gcd = action
        self.wait_ms = None
        self.gcd_calls.append(action)
        self.raw.append(RawSink("gcd", operation, value, source_ref))

    def emit_known_noop_gcd_attempt(
        self,
        action: str,
        *,
        operation: str,
        value: str,
        source_ref: str,
        reason: str,
    ) -> None:
        """Keep a source call that the frozen client configuration rejects.

        The raw lane remains auditable, while the normalized executable lane is
        left unchanged (normally WAIT).  This is different from silently
        deleting a source call: ``known_noop_gcd_attempts`` records why it has no
        game-side state transition under the adapter's declared configuration.
        """

        self.gcd_calls.append(action)
        self.raw.append(RawSink("gcd", operation, value, source_ref))
        self.known_noop_gcd_attempts.append(
            {
                "action": action,
                "operation": operation,
                "source_ref": source_ref,
                "reason": reason,
            }
        )

    def emit_queue(
        self,
        queue: SwingQueueOp,
        *,
        operation: str,
        value: str,
        source_ref: str,
    ) -> None:
        self.queue = queue
        self.raw.append(RawSink("swing_queue", operation, value, source_ref))

    def emit_off_gcd(
        self,
        action: str,
        *,
        operation: str,
        value: str,
        source_ref: str,
    ) -> None:
        self.off_gcd.append(action)
        self.raw.append(RawSink("off_gcd", operation, value, source_ref))

    def emit_stance(
        self,
        stance: StanceOp,
        *,
        operation: str,
        value: str,
        source_ref: str,
    ) -> None:
        self.stance = stance
        self.raw.append(RawSink("stance", operation, value, source_ref))

    def emit_stop_cast(self, *, source_ref: str) -> None:
        self.cast_control = CastControl.STOP_CAST
        self.raw.append(
            RawSink("cast_control", "SpellStopCasting", None, source_ref)
        )

    def build(
        self,
        provenance: ExpertProvenance,
        *,
        role_can_vote: bool,
        reason: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> ExpertDecision:
        combined_metadata = dict(metadata or {})
        combined_metadata.setdefault(
            "normalized_gcd_resolution",
            (
                "last_state_effecting_source_gcd_sink"
                if self.known_noop_gcd_attempts
                else "last_raw_gcd_sink"
            ),
        )
        combined_metadata.setdefault("raw_gcd_calls", list(self.gcd_calls))
        if self.known_noop_gcd_attempts:
            combined_metadata.setdefault(
                "known_noop_source_gcd_attempts",
                list(self.known_noop_gcd_attempts),
            )
        return ExpertDecision(
            provenance=provenance,
            valid=True,
            gcd=self.gcd,
            wait_ms=self.wait_ms,
            swing_queue=self.queue,
            off_gcd=tuple(self.off_gcd),
            stance=self.stance,
            target=self.target,
            cast_control=self.cast_control,
            raw_sink_order=tuple(self.raw),
            eligible_for_independent_vote=role_can_vote,
            reason=reason,
            metadata=combined_metadata,
        )


class CatFurySourceAdapter:
    """Source-derived Cat Fury proposal with current profile 1 frozen."""

    expert_id = "cat.fury.profile1"
    profile = CAT_FURY_PROFILE1

    def _provenance(self) -> ExpertProvenance:
        return ExpertProvenance(
            expert_id=self.expert_id,
            kind=ProvenanceKind.SOURCE_DERIVED,
            role=ExpertRole.DEPLOYED,
            authority_files=(
                r"%WOW_ADDONS_ROOT%\Cat\Cat.toc",
                r"%WOW_ADDONS_ROOT%\Cat\WarriorFury.lua",
                r"%WOW_CHARACTER_SAVEDVARIABLES%\Cat.lua",
            ),
            source_refs=(
                "WarriorFury.lua:47",
                "WarriorFury.lua:338-388",
                "WarriorFury.lua:397-618",
                "SavedVariables/Cat.lua:619-668",
            ),
        )

    def propose(self, state: FuryExpertState) -> ExpertDecision:
        provenance = self._provenance()
        if not state.target_exists:
            return invalid_decision(
                provenance,
                "current profile target_mode=0 does not define an offline target",
                metadata={"profile_index": 1, "bounded_scope": "living target"},
            )

        builder = _DecisionBuilder()
        builder.raw.append(
            RawSink(
                "autoattack",
                "MPStartAttack",
                "START",
                "WarriorFury.lua:83-87",
            )
        )

        if (
            self.profile.battle_shout_enabled
            and not state.has_battle_shout
            and state.rage > 9
            and state.battle_shout_remaining_s < 5.0
        ):
            builder.emit_gcd(
                BATTLE_SHOUT,
                operation="CastSpellByName",
                value="战斗怒吼",
                source_ref="WarriorFury.lua:283-289",
            )
            return builder.build(
                provenance,
                role_can_vote=True,
                reason="Cat profile 1 refreshes Battle Shout before its Fury rotation",
                metadata=self._metadata(state),
            )

        if (
            self.profile.berserker_stance_required
            and state.in_combat
            and state.current_stance is not StanceOp.BERSERKER
        ):
            builder.emit_stance(
                StanceOp.BERSERKER,
                operation="CastSpellByName",
                value="狂暴姿态",
                source_ref="WarriorFury.lua:292-295",
            )
            return builder.build(
                provenance,
                role_can_vote=True,
                reason="Cat profile 1 requires Berserker Stance before rotation actions",
                metadata=self._metadata(state),
            )

        if self._should_bloodrage(state):
            builder.emit_off_gcd(
                BLOODRAGE,
                operation="MPCastWithNampower",
                value="血性狂暴",
                source_ref="WarriorFury.lua:299-303",
            )

        queue, reserve = self._queue_proposal(state)
        if queue is not SwingQueueOp.KEEP:
            builder.emit_queue(
                queue,
                operation="MPCastWithNampower",
                value="顺劈斩" if queue is SwingQueueOp.CLEAVE else "英勇打击",
                source_ref=(
                    "WarriorFury.lua:338-364"
                    if state.weapon_mode is WeaponMode.TWO_HAND
                    else "WarriorFury.lua:370-386"
                ),
            )

        if state.gcd_ready:
            if state.weapon_mode is WeaponMode.DUAL_WIELD:
                self._cat_dual_wield_gcd(builder, state)
            else:
                self._cat_two_hand_gcd(builder, state)

        metadata = self._metadata(state)
        metadata["computed_queue_reserve"] = reserve
        metadata["deployed_cancel_helper_active"] = False
        return builder.build(
            provenance,
            role_can_vote=True,
            reason="bounded translation of installed Cat Fury profile 1",
            metadata=metadata,
        )

    def _metadata(self, state: FuryExpertState) -> dict[str, Any]:
        return {
            "profile_index": self.profile.profile_index,
            "profile_frozen": True,
            "weapon_mode": state.weapon_mode.value,
            "source_execution": False,
            "supported_scope": (
                "profile1 target/battle-shout/stance/bloodrage, dynamic queue, "
                "and core two-hand or dual-wield Fury actions"
            ),
        }

    def _should_bloodrage(self, state: FuryExpertState) -> bool:
        if not (
            self.profile.bloodrage_enabled
            and state.bloodrage_ready
            and state.in_combat
            and state.in_melee_range
            and (
                not self.profile.berserker_stance_required
                or state.current_stance is StanceOp.BERSERKER
            )
        ):
            return False
        bloodthirst_need = (
            state.bloodthirst_known
            and state.cooldown_within(state.bloodthirst_ready_in_s, 1.5)
            and state.rage < 30
        )
        whirlwind_need = (
            self.profile.whirlwind_enabled
            and state.cooldown_within(state.whirlwind_ready_in_s, 1.5)
            and state.rage < 25
        )
        return bloodthirst_need or whirlwind_need

    def _queue_proposal(
        self, state: FuryExpertState
    ) -> tuple[SwingQueueOp, float]:
        aoe = self.profile.nearby_enemy_switch_enabled and state.nearby_enemies > 1
        queue = SwingQueueOp.CLEAVE if aoe else SwingQueueOp.HEROIC_STRIKE
        queue_cost = state.cleave_cost if aoe else state.heroic_strike_cost
        if self.profile.heroic_strike_mode == "FIXED":
            reserve = self.profile.heroic_strike_fixed_threshold
        elif state.weapon_mode is WeaponMode.TWO_HAND:
            reserve = 15.0 + queue_cost
            if (
                not aoe
                and state.bloodthirst_known
                and state.cooldown_within(state.bloodthirst_ready_in_s, 1.5)
            ):
                reserve += 30.0
            if (
                self.profile.whirlwind_enabled
                and state.cooldown_within(state.whirlwind_ready_in_s, 1.5)
            ):
                reserve += state.whirlwind_cost
        else:
            reserve = queue_cost * 2.0
            if (
                state.bloodthirst_known
                and state.cooldown_within(state.bloodthirst_ready_in_s, 1.3)
            ):
                reserve += 30.0
            if (
                self.profile.whirlwind_enabled
                and state.cooldown_within(state.whirlwind_ready_in_s, 1.3)
            ):
                reserve += state.whirlwind_cost

        health_allows = aoe and state.weapon_mode is WeaponMode.DUAL_WIELD
        health_allows = health_allows or state.target_health_pct >= 20.0
        if state.rage > reserve and health_allows:
            return queue, reserve
        # Cat's deployed calls to MPWarriorCancelHeroic are commented out.
        return SwingQueueOp.KEEP, reserve

    def _cat_dual_wield_gcd(
        self, builder: _DecisionBuilder, state: FuryExpertState
    ) -> None:
        normal_phase = state.target_health_pct >= 20.0 or not self.profile.execute_enabled
        if normal_phase:
            if (
                state.bloodthirst_known
                and state.cooldown_ready(state.bloodthirst_ready_in_s)
                and state.rage > 29
            ):
                builder.emit_gcd(
                    BLOODTHIRST,
                    operation="CastSpellByName",
                    value="嗜血",
                    source_ref="WarriorFury.lua:580-583",
                )
                return
            if (
                self.profile.whirlwind_enabled
                and state.cooldown_ready(state.whirlwind_ready_in_s)
                and state.rage >= state.whirlwind_cost
                and state.in_melee_range
                and state.current_stance is StanceOp.BERSERKER
            ):
                builder.emit_gcd(
                    WHIRLWIND,
                    operation="CastSpellByName",
                    value="旋风斩",
                    source_ref="WarriorFury.lua:586-589",
                )
                return
            if (
                self.profile.hamstring_enabled
                and state.bloodthirst_ready_in_s > 1.4
                and (
                    not self.profile.whirlwind_enabled
                    or state.whirlwind_ready_in_s > 1.4
                )
                and state.rage > 9
            ):
                builder.emit_gcd(
                    HAMSTRING,
                    operation="CastSpellByName",
                    value="断筋",
                    source_ref="WarriorFury.lua:592-600",
                )
            return

        if (
            state.bloodthirst_known
            and state.cooldown_ready(state.bloodthirst_ready_in_s)
            and state.rage >= state.execute_cost + 30.0
        ):
            builder.emit_gcd(
                BLOODTHIRST,
                operation="CastSpellByName",
                value="嗜血",
                source_ref="WarriorFury.lua:609-610",
            )
            return
        if state.rage >= state.execute_cost:
            builder.emit_gcd(
                EXECUTE,
                operation="CastSpellByName",
                value="斩杀",
                source_ref="WarriorFury.lua:616",
            )

    def _cat_two_hand_gcd(
        self, builder: _DecisionBuilder, state: FuryExpertState
    ) -> None:
        def ready_whirlwind() -> bool:
            return (
                self.profile.whirlwind_enabled
                and state.cooldown_ready(state.whirlwind_ready_in_s)
                and state.rage >= state.whirlwind_cost
                and state.in_melee_range
                and state.current_stance is StanceOp.BERSERKER
            )

        def ready_bloodthirst(minimum_rage: float = 29.0) -> bool:
            return (
                state.bloodthirst_known
                and state.cooldown_ready(state.bloodthirst_ready_in_s)
                and state.rage > minimum_rage
            )

        if state.nearby_enemies > 1 and ready_whirlwind():
            if state.casting_slam:
                builder.emit_stop_cast(source_ref="WarriorFury.lua:400-403")
            builder.emit_gcd(
                WHIRLWIND,
                operation="CastSpellByName",
                value="旋风斩",
                source_ref="WarriorFury.lua:400-404",
            )
            return

        if (
            not state.target_is_boss
            and state.target_health_pct < 20.0
            and self.profile.execute_enabled
            and self.profile.execute_non_boss
        ):
            if state.casting_slam:
                builder.emit_stop_cast(source_ref="WarriorFury.lua:407-410")
            builder.emit_gcd(
                EXECUTE,
                operation="CastSpellByName",
                value="斩杀",
                source_ref="WarriorFury.lua:407-411",
            )
            return

        timing_branch = not state.flurry_talent or state.flurry_active
        if timing_branch:
            if state.mainhand_swing_remaining_s < self.profile.slam_timing_s:
                if ready_whirlwind():
                    builder.emit_gcd(
                        WHIRLWIND,
                        operation="CastSpellByName",
                        value="旋风斩",
                        source_ref="WarriorFury.lua:469-477",
                    )
                    return
                if ready_bloodthirst():
                    builder.emit_gcd(
                        BLOODTHIRST,
                        operation="CastSpellByName",
                        value="嗜血",
                        source_ref="WarriorFury.lua:479-482",
                    )
                    return
                if (
                    state.target_health_pct < 20.0
                    and self.profile.execute_enabled
                    and state.rage >= state.execute_cost
                ):
                    builder.emit_gcd(
                        EXECUTE,
                        operation="CastSpellByName",
                        value="斩杀",
                        source_ref="WarriorFury.lua:484-487",
                    )
                return
            if state.mainhand_swing_remaining_s < 2.0:
                if ready_whirlwind():
                    builder.emit_gcd(
                        WHIRLWIND,
                        operation="CastSpellByName",
                        value="旋风斩",
                        source_ref="WarriorFury.lua:489-494",
                    )
                    return
                if ready_bloodthirst():
                    builder.emit_gcd(
                        BLOODTHIRST,
                        operation="CastSpellByName",
                        value="嗜血",
                        source_ref="WarriorFury.lua:496-499",
                    )
                    return
                if (
                    state.target_health_pct < 20.0
                    and self.profile.execute_enabled
                    and state.rage >= state.execute_cost
                ):
                    builder.emit_gcd(
                        EXECUTE,
                        operation="CastSpellByName",
                        value="斩杀",
                        source_ref="WarriorFury.lua:501-504",
                    )
                    return
                if state.rage >= 15.0:
                    builder.emit_gcd(
                        SLAM,
                        operation="MPCastWithoutNampower",
                        value="猛击",
                        source_ref="WarriorFury.lua:506-508",
                    )
                return
            if state.rage >= 15.0:
                builder.emit_gcd(
                    SLAM,
                    operation="MPCastWithoutNampower",
                    value="猛击",
                    source_ref="WarriorFury.lua:511-515",
                )
            return

        if ready_whirlwind():
            builder.emit_gcd(
                WHIRLWIND,
                operation="CastSpellByName",
                value="旋风斩",
                source_ref="WarriorFury.lua:520-524",
            )
            return
        if (
            state.target_health_pct < 20.0
            and ready_bloodthirst(state.execute_cost + 29.0)
        ):
            builder.emit_gcd(
                BLOODTHIRST,
                operation="CastSpellByName",
                value="嗜血",
                source_ref="WarriorFury.lua:527-530",
            )
            return
        if (
            state.target_health_pct < 20.0
            and self.profile.execute_enabled
            and state.rage >= state.execute_cost
        ):
            builder.emit_gcd(
                EXECUTE,
                operation="CastSpellByName",
                value="斩杀",
                source_ref="WarriorFury.lua:532-535",
            )
            return
        if ready_bloodthirst():
            builder.emit_gcd(
                BLOODTHIRST,
                operation="CastSpellByName",
                value="嗜血",
                source_ref="WarriorFury.lua:537-540",
            )
            return
        if (
            self.profile.hamstring_enabled
            and state.bloodthirst_ready_in_s > 1.5
            and state.whirlwind_ready_in_s > 1.5
            and state.rage > 9
        ):
            builder.emit_gcd(
                HAMSTRING,
                operation="CastSpellByName",
                value="断筋",
                source_ref="WarriorFury.lua:542-546",
            )
            return
        if state.mainhand_swing_remaining_s > 2.0 and state.rage >= 15.0:
            builder.emit_gcd(
                SLAM,
                operation="MPCastWithoutNampower",
                value="猛击",
                source_ref="WarriorFury.lua:549-563",
            )


class ContraDeployedSourceAdapter:
    """Bug-preserving bounded translation of the installed Contra policy."""

    expert_id = "contra.deployed.fury.raid_a"
    xuanfeng = False
    interrupt = False
    nampower_queue_spells_on_cooldown = False

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
                "Contra_ALL.lua:31277-31346",
                "Contra_ALL.lua:31612-31674",
                "Contra_ALL.lua:32049-32074",
                "Contra_ALL.lua:36383-36431",
                "WTF/Config.wtf:112 (NP_QueueSpellsOnCooldown=0)",
            ),
        )

    def propose(self, state: FuryExpertState) -> ExpertDecision:
        provenance = self._provenance()
        if not state.target_exists:
            return invalid_decision(
                provenance,
                "current Contra autoselect=false has no target to evaluate",
                metadata=self._metadata(state),
            )

        builder = _DecisionBuilder()
        builder.raw.append(
            RawSink(
                "autoattack",
                "Contra.StartAttack",
                "START",
                self._ref("prelude"),
            )
        )
        if state.contra_interrupt_action:
            self._emit_contra_gcd(
                builder,
                state,
                state.contra_interrupt_action,
                self._ref("interrupt"),
            )
            return builder.build(
                provenance,
                role_can_vote=True,
                reason="Contra interrupt prelude returned before the Fury body",
                metadata=self._metadata(state),
            )
        for action in state.contra_prelude_off_gcd:
            builder.emit_off_gcd(
                action,
                operation="Contra prelude helper",
                value=action,
                source_ref=self._ref("prelude"),
            )
        if not state.has_shield:
            builder.emit_stance(
                StanceOp.BERSERKER,
                operation="ContraZSCast",
                value="狂暴姿态",
                source_ref=self._ref("stance"),
            )

        if state.weapon_mode is WeaponMode.TWO_HAND:
            if not (state.target_is_boss or state.target_is_training_dummy):
                return invalid_decision(
                    provenance,
                    "current adapter lacks Contra non-boss max-health branch inputs",
                    metadata=self._metadata(state),
                )
            self._two_hand(builder, state)
        else:
            self._dual_wield(builder, state)
        return builder.build(
            provenance,
            role_can_vote=True,
            reason="bounded bug-preserving translation of TOC-loaded Contra_ALL.lua",
            metadata=self._metadata(state),
        )

    def _metadata(self, state: FuryExpertState) -> dict[str, Any]:
        return {
            "toc_loaded_file": "Contra_ALL.lua",
            "saved_mode": "副本模式",
            "saved_xuanfeng": self.xuanfeng,
            "saved_baofa": True,
            "weapon_mode": state.weapon_mode.value,
            "source_execution": False,
            "gcd_cast_api": "QueueSpellByName (Slam uses CastSpellByName)",
            "nampower_queue_spells_on_cooldown": (
                self.nampower_queue_spells_on_cooldown
            ),
            "known_noop_retry_wait_ms": 100,
            "always_true_last_cast_disjunction_preserved": True,
            "helper_boundary": (
                "ZS_DaDuan/ZS_BAOF/ZS_FZ/ZS_SC require reconstructed helper "
                "outputs supplied in state"
            ),
        }

    def _ref(self, name: str) -> str:
        if name == "prelude":
            return "Contra_ALL.lua:31614 or 32051"
        if name == "interrupt":
            return "Contra_ALL.lua:31614 or 32051"
        if name == "stance":
            return "Contra_ALL.lua:31615 or 32052"
        return "Contra_ALL.lua"

    def _emit_contra_gcd(
        self,
        builder: _DecisionBuilder,
        state: FuryExpertState,
        action: str,
        source_ref: str,
    ) -> None:
        names = {
            BLOODTHIRST: "嗜血",
            WHIRLWIND: "旋风斩",
            EXECUTE: "斩杀",
            SLAM: "猛击",
            HAMSTRING: "断筋",
            PUMMEL: "拳击",
            SUNDER_ARMOR: "破甲攻击",
        }
        value = names.get(action, action)
        operation = (
            "CastSpellByName"
            if action == SLAM or not state.nampower
            else "QueueSpellByName"
        )
        builder.emit_gcd(
            action,
            operation=operation,
            value=value,
            source_ref=source_ref,
        )

    def _emit_known_noop_contra_gcd(
        self,
        builder: _DecisionBuilder,
        state: FuryExpertState,
        action: str,
        source_ref: str,
        *,
        reason: str,
    ) -> None:
        names = {BLOODTHIRST: "嗜血"}
        operation = "CastSpellByName" if not state.nampower else "QueueSpellByName"
        builder.emit_known_noop_gcd_attempt(
            action,
            operation=operation,
            value=names.get(action, action),
            source_ref=source_ref,
            reason=reason,
        )

    def _emit_heroic_strike(
        self,
        builder: _DecisionBuilder,
        state: FuryExpertState,
        source_ref: str,
    ) -> None:
        if state.queued_swing is SwingQueueOp.HEROIC_STRIKE:
            return
        builder.emit_queue(
            SwingQueueOp.HEROIC_STRIKE,
            operation="QueueSpellByName" if state.nampower else "CastSpellByName",
            value="英勇打击",
            source_ref=source_ref,
        )

    def _two_hand(self, builder: _DecisionBuilder, state: FuryExpertState) -> None:
        in_ww_range = state.contra_whirlwind_range()
        if not state.flurry_active and state.target_health_pct > 70.0:
            if self.xuanfeng and in_ww_range:
                self._emit_contra_gcd(
                    builder, state, WHIRLWIND, self._two_ref(31618)
                )
            if not self.xuanfeng and state.bloodthirst_ready_in_s == 0.0:
                self._emit_contra_gcd(
                    builder, state, BLOODTHIRST, self._two_ref(31621)
                )

        # The three ~= terms are joined with OR in the deployed source, hence
        # this block is true for every possible LastCastName.  Do not simplify
        # it into the likely intended AND semantics.
        if state.target_health_pct > 20.0:
            if self.xuanfeng and in_ww_range:
                self._emit_contra_gcd(
                    builder, state, WHIRLWIND, self._two_ref(31625)
                )
            if not self.xuanfeng:
                self._emit_contra_gcd(
                    builder, state, BLOODTHIRST, self._two_ref(31628)
                )

        if state.contra_zssdw >= 3:
            queue_condition = (
                state.target_health_pct > 20.0
                and (
                    (state.rage > 82.0 and state.contra_st_s > 1.0)
                    or (state.rage > 67.0 and state.contra_st_s < 1.0)
                )
            )
            queue_ref = self._two_ref(31631)
        else:
            queue_condition = (
                state.target_health_pct > 20.0
                and (
                    (state.rage > 87.0 and state.contra_st_s > 1.0)
                    or (state.rage > 72.0 and state.contra_st_s < 1.0)
                )
            )
            queue_ref = self._two_ref(31634)
        if queue_condition:
            self._emit_heroic_strike(builder, state, queue_ref)

        if state.target_health_pct > 20.0:
            slam_condition = False
            if self.xuanfeng:
                slam_condition = state.slam_remaining_s == 0.0 and (
                    state.last_cast_name in {BLOODTHIRST, WHIRLWIND}
                    or (
                        state.bloodthirst_ready_in_s != 0.0
                        and state.whirlwind_ready_in_s != 0.0
                        and state.contra_st_s > 1.2
                    )
                )
            else:
                slam_condition = state.last_cast_name == BLOODTHIRST or (
                    state.bloodthirst_ready_in_s != 0.0
                    and state.contra_st_s > 1.2
                )
            if slam_condition:
                self._emit_contra_gcd(builder, state, SLAM, self._two_ref(31638))
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
                self._emit_contra_gcd(builder, state, EXECUTE, self._two_ref(31653))
            if (
                state.contra_st_s > state.slam_cast_time_s + 0.3
                and state.bloodthirst_ready_in_s != 0.0
            ) or (
                state.rage <= 44.0
                and state.contra_st_s > state.slam_cast_time_s + 0.3
                and state.bloodthirst_ready_in_s == 0.0
            ):
                self._emit_contra_gcd(builder, state, SLAM, self._two_ref(31654))
        if state.target_health_pct <= 2.0:
            if state.slam_remaining_s > 0.2:
                builder.emit_stop_cast(source_ref=self._two_ref(31656))
            self._emit_contra_gcd(builder, state, EXECUTE, self._two_ref(31659))

    def _dual_wield(self, builder: _DecisionBuilder, state: FuryExpertState) -> None:
        if (
            state.target_health_pct <= 20.0
            and state.rage > 59.0
            and state.target_is_boss
        ) or (state.target_health_pct <= 20.0 and not state.target_is_boss):
            self._emit_contra_gcd(builder, state, EXECUTE, self._dual_ref(32053))
        if (
            state.target_health_pct > 21.0
            and state.rage > 59.0
        ) or (
            state.target_health_pct > 21.0
            and state.bloodthirst_ready_in_s > state.contra_sd_s
            and state.rage > 47.0
        ):
            self._emit_heroic_strike(builder, state, self._dual_ref(32056))
        if (
            state.target_health_pct > 21.0 and state.contra_ss_s < 0.9
        ) or (
            1.0 < state.target_health_pct < 19.0
            and 29.0 < state.rage < 45.0
            and state.target_is_boss
            and state.contra_ss_s < 0.5
        ):
            if (
                state.bloodthirst_known
                and state.cooldown_ready(state.bloodthirst_ready_in_s)
                and state.rage >= 30.0
            ):
                self._emit_contra_gcd(
                    builder, state, BLOODTHIRST, self._dual_ref(32062)
                )
            else:
                noop_reasons: list[str] = []
                if not state.bloodthirst_known:
                    noop_reasons.append("bloodthirst_unknown")
                if not state.cooldown_ready(state.bloodthirst_ready_in_s):
                    noop_reasons.append("bloodthirst_on_cooldown")
                if state.rage < 30.0:
                    noop_reasons.append("rage_below_30")
                self._emit_known_noop_contra_gcd(
                    builder,
                    state,
                    BLOODTHIRST,
                    self._dual_ref(32062),
                    reason=(
                        "NP_QueueSpellsOnCooldown=0;" + ",".join(noop_reasons)
                    ),
                )
        if (
            self.xuanfeng
            and state.target_health_pct > 21.0
            and state.bloodthirst_ready_in_s > 1.5
            and state.contra_ss_s < 0.9
            and state.rage > 34.0
            and state.contra_whirlwind_range()
        ):
            self._emit_contra_gcd(builder, state, WHIRLWIND, self._dual_ref(32065))
        filler = (
            state.target_health_pct > 21.0
            and state.bloodthirst_ready_in_s > 1.4
            and state.whirlwind_ready_in_s > 1.4
            and state.rage > 69.0
            and state.contra_ss_s < 0.9
        )
        if (
            not self.interrupt
            and filler
            and state.target_name != "麦迪文的回响"
        ):
            self._emit_contra_gcd(builder, state, PUMMEL, self._dual_ref(32068))
        if filler:
            self._emit_contra_gcd(builder, state, HAMSTRING, self._dual_ref(32071))

    def _two_ref(self, line: int) -> str:
        return f"Contra_ALL.lua:{line}"

    def _dual_ref(self, line: int) -> str:
        return f"Contra_ALL.lua:{line}"


class ContraNewCandidateAdapter(ContraDeployedSourceAdapter):
    """Readable contra_new rules, explicitly excluded as an independent vote."""

    expert_id = "contra_new.fury.candidate"

    def _provenance(self) -> ExpertProvenance:
        return ExpertProvenance(
            expert_id=self.expert_id,
            kind=ProvenanceKind.SOURCE_DERIVED,
            role=ExpertRole.CANDIDATE,
            authority_files=(
                r"%BRAIN_OF_CAT_ROOT%\Contra_new\Contra.toc",
                r"%BRAIN_OF_CAT_ROOT%\Contra_new\Contra_Macro.lua",
                r"%BRAIN_OF_CAT_ROOT%\Contra_new\Contra_Scrip_Warrior.lua",
            ),
            source_refs=(
                "Contra_new/Contra_Macro.lua:101-158",
                "Contra_new/Contra_Scrip_Warrior.lua:586-651",
                "Contra_new/Contra_Scrip_Warrior.lua:1257-1288",
            ),
        )

    def propose(self, state: FuryExpertState) -> ExpertDecision:
        provenance = self._provenance()
        if not state.target_exists:
            return invalid_decision(
                provenance,
                "contra_new candidate has no target to evaluate",
                metadata=self._metadata(state),
            )
        builder = _DecisionBuilder()
        builder.raw.append(
            RawSink(
                "autoattack",
                "Contra.StartAttack",
                "START",
                self._ref("prelude"),
            )
        )
        if state.contra_interrupt_action:
            self._emit_contra_gcd(
                builder,
                state,
                state.contra_interrupt_action,
                self._ref("interrupt"),
            )
            return builder.build(
                provenance,
                role_can_vote=False,
                reason="contra_new candidate interrupt prelude",
                metadata=self._metadata(state),
            )
        for action in state.contra_prelude_off_gcd:
            builder.emit_off_gcd(
                action,
                operation="Contra prelude helper",
                value=action,
                source_ref=self._ref("prelude"),
            )
        if not state.has_shield:
            builder.emit_stance(
                StanceOp.BERSERKER,
                operation="ContraZSCast",
                value="狂暴姿态",
                source_ref=self._ref("stance"),
            )
        if state.weapon_mode is WeaponMode.TWO_HAND:
            if not (state.target_is_boss or state.target_is_training_dummy):
                return invalid_decision(
                    provenance,
                    "current contra_new adapter lacks non-boss max-health branch inputs",
                    metadata=self._metadata(state),
                )
            self._two_hand(builder, state)
        else:
            if state.sunder_action_required:
                self._emit_contra_gcd(
                    builder, state, SUNDER_ARMOR, self._dual_ref(1265)
                )
            else:
                self._dual_wield(builder, state)
        return builder.build(
            provenance,
            role_can_vote=False,
            reason="contra_new is a non-runnable candidate rule source, not a vote",
            metadata=self._metadata(state),
        )

    def _metadata(self, state: FuryExpertState) -> dict[str, Any]:
        metadata = super()._metadata(state)
        metadata.pop("known_noop_retry_wait_ms", None)
        metadata.update(
            {
                "toc_loaded_file": None,
                "gcd_cast_api": "CastSpellByName",
                "nampower_queue_spells_on_cooldown": None,
                "standalone_runnable": False,
                "missing_toc_members": ["Contra_Debuff.lua", "Contra_UI_DB.lua"],
                "candidate_only": True,
                "independent_vote_allowed": False,
            }
        )
        return metadata

    def _emit_contra_gcd(
        self,
        builder: _DecisionBuilder,
        state: FuryExpertState,
        action: str,
        source_ref: str,
    ) -> None:
        names = {
            BLOODTHIRST: "嗜血",
            WHIRLWIND: "旋风斩",
            EXECUTE: "斩杀",
            SLAM: "猛击",
            HAMSTRING: "断筋",
            PUMMEL: "拳击",
            SUNDER_ARMOR: "破甲攻击",
        }
        builder.emit_gcd(
            action,
            operation="CastSpellByName",
            value=names.get(action, action),
            source_ref=source_ref,
        )

    def _emit_heroic_strike(
        self,
        builder: _DecisionBuilder,
        state: FuryExpertState,
        source_ref: str,
    ) -> None:
        if state.queued_swing is SwingQueueOp.HEROIC_STRIKE:
            return
        builder.emit_queue(
            SwingQueueOp.HEROIC_STRIKE,
            operation="CastSpellByName",
            value="英勇打击",
            source_ref=source_ref,
        )

    def _dual_wield(self, builder: _DecisionBuilder, state: FuryExpertState) -> None:
        if (
            state.target_health_pct <= 20.0
            and state.rage > 69.0
            and state.target_is_boss
        ) or (state.target_health_pct <= 20.0 and not state.target_is_boss):
            self._emit_contra_gcd(builder, state, EXECUTE, self._dual_ref(1269))
        if (
            state.target_health_pct > 20.0
            and state.rage > 59.0
        ) or (
            state.target_health_pct > 21.0
            and state.bloodthirst_ready_in_s > state.contra_sd_s
            and state.rage > 47.0
        ):
            self._emit_heroic_strike(builder, state, self._dual_ref(1273))
        if (
            state.target_health_pct > 21.0 and state.contra_ss_s < 0.9
        ) or (
            1.0 < state.target_health_pct < 19.0
            and 29.0 < state.rage < 45.0
            and state.target_is_boss
            and state.contra_ss_s < 0.5
        ):
            self._emit_contra_gcd(builder, state, BLOODTHIRST, self._dual_ref(1277))
        if (
            self.xuanfeng
            and state.target_health_pct > 21.0
            and state.bloodthirst_ready_in_s > 1.5
            and state.contra_ss_s < 0.9
            and state.rage > 34.0
            and state.contra_whirlwind_range()
        ):
            self._emit_contra_gcd(builder, state, WHIRLWIND, self._dual_ref(1281))
        if (
            state.target_health_pct > 21.0
            and state.bloodthirst_ready_in_s > 1.4
            and state.whirlwind_ready_in_s > 1.4
            and state.rage > 69.0
            and state.contra_ss_s < 0.9
            and not state.has_shield_break_trinket_buff
        ):
            self._emit_contra_gcd(builder, state, HAMSTRING, self._dual_ref(1285))

    def _ref(self, name: str) -> str:
        if name in {"prelude", "interrupt"}:
            return "Contra_new/Contra_Scrip_Warrior.lua:590 or 1261"
        if name == "stance":
            return "Contra_new/Contra_Scrip_Warrior.lua:592 or 1263"
        return "Contra_new/Contra_Scrip_Warrior.lua"

    def _two_ref(self, line: int) -> str:
        # The candidate body contains extra whitespace/comments, so its line
        # offsets are not constant relative to the deployed monolith.
        mapped = {
            31618: 597,
            31621: 601,
            31625: 606,
            31628: 610,
            31631: 614,
            31634: 618,
            31638: 624,
            31644: 632,
            31647: 636,
            31652: 642,
            31653: 643,
            31654: 644,
            31656: 647,
            31659: 651,
        }[line]
        return f"Contra_new/Contra_Scrip_Warrior.lua:{mapped}"

    def _dual_ref(self, line: int) -> str:
        return f"Contra_new/Contra_Scrip_Warrior.lua:{line}"


@dataclass(frozen=True)
class CuratedCat2Card:
    """One explicitly supplied Cat2 card and its saved step options."""

    card_id: str
    options: Mapping[str, float] = field(default_factory=dict)


class Cat2ProfileAdapter:
    """Interpret an explicit card stack; never fabricate the missing live profile."""

    def __init__(
        self,
        card_stack: Sequence[CuratedCat2Card] | None = None,
        *,
        profile_name: str | None = None,
        expert_id: str | None = None,
    ) -> None:
        self.card_stack = None if card_stack is None else tuple(card_stack)
        self.profile_name = profile_name
        self.expert_id = expert_id or (
            "cat2.fury.profile"
            if self.card_stack is None
            else "cat2.fury.curated_candidate"
        )

    def _provenance(self) -> ExpertProvenance:
        role = (
            ExpertRole.UNAVAILABLE
            if self.card_stack is None
            else ExpertRole.CANDIDATE
        )
        return ExpertProvenance(
            expert_id=self.expert_id,
            kind=ProvenanceKind.SOURCE_DERIVED,
            role=role,
            authority_files=(
                r"%WOW_ADDONS_ROOT%\Cat2\Cat2.toc",
                r"%WOW_ADDONS_ROOT%\Cat2\Core\ConfigurationRunner.lua",
                r"%WOW_CHARACTER_SAVEDVARIABLES%\Cat2.lua",
            ),
            source_refs=(
                "ConfigurationRunner.lua:260-389",
                "ConfigurationRunner.lua:436-448",
                "SavedVariables/Cat2.lua:2-16",
            ),
        )

    def propose(self, state: FuryExpertState) -> ExpertDecision:
        provenance = self._provenance()
        if self.card_stack is None:
            return invalid_decision(
                provenance,
                "no_profile: current Cat2 SavedVariables has no configurations",
                metadata={
                    "profile_name": None,
                    "live_profile": False,
                    "candidate_only": False,
                },
            )
        if not state.target_exists:
            return invalid_decision(
                provenance,
                "curated Cat2 stack has no target",
                metadata=self._metadata(),
            )

        builder = _DecisionBuilder()
        for card in self.card_stack:
            if self._execute_card(builder, state, card):
                break
        return builder.build(
            provenance,
            role_can_vote=False,
            reason="explicit curated Cat2 card stack; not a deployed live profile",
            metadata=self._metadata(),
        )

    def _metadata(self) -> dict[str, Any]:
        return {
            "profile_name": self.profile_name,
            "live_profile": False,
            "candidate_only": True,
            "independent_vote_allowed": False,
            "card_stack": (
                []
                if self.card_stack is None
                else [card.card_id for card in self.card_stack]
            ),
        }

    def _execute_card(
        self,
        builder: _DecisionBuilder,
        state: FuryExpertState,
        card: CuratedCat2Card,
    ) -> bool:
        options = card.options
        card_id = card.card_id
        if card_id == "warrior_heroic_strike":
            threshold = float(options.get("rageThreshold", 50.0))
            if state.rage >= threshold:
                builder.emit_queue(
                    SwingQueueOp.HEROIC_STRIKE,
                    operation="Cat2.Cast",
                    value="英勇打击",
                    source_ref="Cards/Warrior/HeroicStrike.lua:35-49",
                )
            return False
        if card_id == "warrior_cleave":
            threshold = float(options.get("rageThreshold", 50.0))
            if state.rage >= threshold:
                builder.emit_queue(
                    SwingQueueOp.CLEAVE,
                    operation="Cat2.Cast",
                    value="顺劈斩",
                    source_ref="Cards/Warrior/Cleave.lua:35-49",
                )
            return False
        if card_id == "warrior_bloodrage":
            maximum = float(options.get("maximumRage", 30.0))
            if (
                state.in_combat
                and state.in_melee_range
                and state.rage < maximum
                and state.bloodrage_ready
            ):
                builder.emit_off_gcd(
                    BLOODRAGE,
                    operation="Cat2.Cast",
                    value="血性狂暴",
                    source_ref="Cards/Warrior/Bloodrage.lua:35-51",
                )
            return False
        if card_id == "warrior_berserker_stance":
            if state.current_stance is not StanceOp.BERSERKER:
                builder.emit_stance(
                    StanceOp.BERSERKER,
                    operation="CastShapeshiftForm/Cat2.Cast",
                    value="狂暴姿态",
                    source_ref="Cards/Warrior/BerserkerStance.lua:29-47",
                )
                return True
            return False
        if card_id == "warrior_death_wish":
            if state.in_melee_range and state.rage >= 10.0 and state.death_wish_ready:
                builder.emit_off_gcd(
                    DEATH_WISH,
                    operation="Cat2.Cast",
                    value="死亡之愿",
                    source_ref="Cards/Warrior/DeathWish.lua:24-40",
                )
                return True
            return False
        if card_id == "warrior_bloodthirst":
            if (
                state.rage >= 30.0
                and state.cooldown_ready(state.bloodthirst_ready_in_s)
            ):
                builder.emit_gcd(
                    BLOODTHIRST,
                    operation="Cat2.Cast",
                    value="嗜血",
                    source_ref="Cards/Warrior/Bloodthirst.lua:24-36",
                )
                return True
            return False
        if card_id == "warrior_whirlwind":
            if (
                state.current_stance is StanceOp.BERSERKER
                and state.target_distance_yards < 8.0
                and state.rage >= state.whirlwind_cost
                and state.cooldown_ready(state.whirlwind_ready_in_s)
            ):
                builder.emit_gcd(
                    WHIRLWIND,
                    operation="Cat2.Cast",
                    value="旋风斩",
                    source_ref="Cards/Warrior/Whirlwind.lua:35-62",
                )
                return True
            return False
        if card_id == "warrior_execute":
            if (
                state.rage >= state.execute_cost
                and state.target_health_pct < 19.9
            ):
                if state.casting_slam and bool(
                    options.get("interruptCastForExecute", 0.0)
                ):
                    builder.emit_stop_cast(
                        source_ref="Cards/Warrior/Execute.lua:42-45"
                    )
                builder.emit_gcd(
                    EXECUTE,
                    operation="Cat2.Cast",
                    value="斩杀",
                    source_ref="Cards/Warrior/Execute.lua:40-48",
                )
                return True
            return False
        if card_id == "warrior_slam":
            minimum = float(options.get("minimumSwingTime", 1.5))
            if state.rage >= 15.0 and state.mainhand_swing_remaining_s > minimum:
                builder.emit_gcd(
                    SLAM,
                    operation="Cat2.Cast",
                    value="猛击",
                    source_ref="Cards/Warrior/Slam.lua:35-51",
                )
                return True
            return False
        raise ValueError(f"unsupported curated Cat2 card: {card_id}")
