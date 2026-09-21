"""Drive teammate wakes during an Incantagos development policy replay.

This binds the outcome-derived Incantagos hypothesis, not a source-bound
evaluation case.  It may exercise action/response mechanics but its receipts
are not performance evidence.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from .responsive_incantagos_development_session_v1 import (
    BoundResponsiveIncantagosDevelopmentSessionV1,
    bind_responsive_incantagos_development_session_v1,
)
from .responsive_team_bridge_adapter_v1 import LoadedResponsiveTeammateModelV1
from .upper_kara_responsive_incantagos_case_v1 import (
    CompiledResponsiveIncantagosCaseV1,
)


TARGET_DIAGNOSTIC_CUTOFF_MS = 9098


class IncantagosDevelopmentDrivenBridgeV1:
    """Own a raw bridge and drain ready teammate events before policy input."""

    def __init__(
        self,
        *,
        bridge: Any,
        case: CompiledResponsiveIncantagosCaseV1,
        loaded_model: LoadedResponsiveTeammateModelV1,
        teammate_seed: int,
        max_responsive_events: int = 100_000,
    ) -> None:
        if max_responsive_events < 1:
            raise ValueError("max_responsive_events must be positive")
        self._bridge = bridge
        self._case = case
        self._loaded_model = loaded_model
        self._teammate_seed = teammate_seed
        self._max_responsive_events = max_responsive_events
        self.session: BoundResponsiveIncantagosDevelopmentSessionV1 | None = None
        self.responsive_event_count = 0
        self.responsive_applied_damage = 0.0
        self.first_wire_receipt: dict[str, Any] | None = None
        self.last_wire_receipt: dict[str, Any] | None = None
        self._target_event_groups: dict[tuple[Any, ...], dict[str, Any]] = {}
        self._first_white_actors: set[str] = set()
        self._first_white_groups: dict[tuple[Any, ...], dict[str, Any]] = {}
        self._start_head_counts = {
            "head": 0, "without_head": 0, "nonhostile_or_untargeted": 0,
        }

    def target_event_diagnostic(self) -> dict[str, Any]:
        """Return small development-only aggregates, never individual events."""

        sort_key = lambda row: (
            row["time_bucket"],
            -1 if row["target_index"] is None else row["target_index"],
            row.get("event_type", ""),
            -1 if row.get("spell_id") is None else row["spell_id"],
            row["target_selection_basis"],
        )
        return {
            "schema": "development_responsive_target_event_diagnostic/v1",
            "cutoff_ms_inclusive": TARGET_DIAGNOSTIC_CUTOFF_MS,
            "event_count": self.responsive_event_count,
            "applied_damage": self.responsive_applied_damage,
            "groups": sorted(
                (dict(row) for row in self._target_event_groups.values()),
                key=sort_key,
            ),
            "first_white_6603_actor_count": len(self._first_white_actors),
            "first_white_6603_groups": sorted(
                (dict(row) for row in self._first_white_groups.values()),
                key=sort_key,
            ),
            "direct_start_head_counts": dict(self._start_head_counts),
        }

    def _record_target_event(self, emitted: Mapping[str, Any]) -> None:
        receipt = emitted["wire_receipt"]
        sampled = emitted["sampled_emission"]
        bucket = (
            "through_9098ms"
            if receipt["time_ms"] <= TARGET_DIAGNOSTIC_CUTOFF_MS
            else "after_9098ms"
        )
        target = receipt["target_index"]
        event_type = receipt["event_type"]
        spell_id = sampled.get("spell_id")
        basis = emitted["target_selection_basis"]
        key = (bucket, target, event_type, spell_id, basis)
        group = self._target_event_groups.setdefault(key, {
            "time_bucket": bucket,
            "target_index": target,
            "event_type": event_type,
            "spell_id": spell_id,
            "target_selection_basis": basis,
            "event_count": 0,
            "applied_hit_count": 0,
            "applied_damage": 0.0,
        })
        group["event_count"] += 1
        if receipt["status"] == "APPLIED":
            damage = float(receipt["applied_damage"])
            if damage > 0:
                group["applied_hit_count"] += 1
            group["applied_damage"] += damage
        if event_type == "DMG" and spell_id == 6603:
            actor = receipt["actor_guid"]
            if actor not in self._first_white_actors:
                self._first_white_actors.add(actor)
                first_key = (bucket, target, basis)
                first = self._first_white_groups.setdefault(first_key, {
                    "time_bucket": bucket,
                    "target_index": target,
                    "target_selection_basis": basis,
                    "actor_count": 0,
                    "applied_damage": 0.0,
                })
                first["actor_count"] += 1
                if receipt["status"] == "APPLIED":
                    first["applied_damage"] += float(receipt["applied_damage"])
        if event_type == "START":
            if target is None or sampled.get("target_mode") not in {
                "STAY_ALIVE", "SWITCH_ALIVE",
            }:
                self._start_head_counts["nonhostile_or_untargeted"] += 1
            else:
                head = emitted["target_choice_head"]
                self._start_head_counts[
                    "head" if isinstance(head, Mapping) and head.get("target_guid") else "without_head"
                ] += 1

    def __enter__(self) -> "IncantagosDevelopmentDrivenBridgeV1":
        self._bridge.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> Any:
        return self._bridge.__exit__(exc_type, exc, traceback)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._bridge, name)

    def load_dynamic_v4(self, request: Mapping[str, Any], seed: int, config: Any) -> Any:
        if self.session is not None:
            raise RuntimeError("development driven bridge already loaded")
        if request != self._case.request or config != self._case.dynamic_config:
            raise ValueError("replay load differs from the bound Incantagos case")
        self.session = bind_responsive_incantagos_development_session_v1(
            bridge=self._bridge,
            case=self._case,
            loaded_model=self._loaded_model,
            simulator_seed=seed,
            teammate_seed=self._teammate_seed,
            pair_id=f"incantagos-v4-development-seed-{seed}",
            branch_id="responsive-program-replay",
            candidate_suffix_id="current-state-program",
        )
        state = self._drain_ready(self._bridge.state())
        return replace(self.session.load_result, state=dict(state))

    def advance(self) -> dict[str, Any]:
        if self.session is None:
            raise RuntimeError("development driven bridge is not loaded")
        return dict(self._drain_ready(self._bridge.advance()))

    def _drain_ready(self, state: Mapping[str, Any]) -> Mapping[str, Any]:
        current = state
        while current.get("wake_ready") is not None:
            if self.responsive_event_count >= self._max_responsive_events:
                raise RuntimeError("responsive development event limit exceeded")
            if self.session is None:
                raise RuntimeError("responsive wake preceded development binding")
            step = self.session.adapter.emit_global_ready_and_rearm()
            emitted = step["emitted"]
            receipt = dict(emitted["wire_receipt"])
            self._record_target_event(emitted)
            self.responsive_event_count += 1
            self.responsive_applied_damage += float(receipt["applied_damage"])
            if self.first_wire_receipt is None:
                self.first_wire_receipt = receipt
            self.last_wire_receipt = receipt
            current = self._bridge.state()
        return current


__all__ = ["IncantagosDevelopmentDrivenBridgeV1"]
