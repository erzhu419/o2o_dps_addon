"""Development-only early direct-white emission trace for one replay."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from .responsive_incantagos_development_session_v1 import (
    bind_responsive_incantagos_development_session_v1,
)
from .responsive_incantagos_driven_bridge_v1 import (
    TARGET_DIAGNOSTIC_CUTOFF_MS,
    IncantagosDevelopmentDrivenBridgeV1,
)

FOCUS_ACTORS = (
    "0x0000000000757D23",
    "0x000000000078D2A6",
    "0x00000000005F5FFA",
)


class White6603ActorTraceDrivenBridgeV1(IncantagosDevelopmentDrivenBridgeV1):
    """Add a small actor-level trace without changing emission or target choice."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._early_direct_white6603: list[dict[str, Any]] = []
        self._focus_actor_wake_marks: dict[str, list[dict[str, Any]]] = {
            actor: [] for actor in FOCUS_ACTORS
        }
        case = kwargs["case"]
        actors = getattr(case, "actors", ())
        teammates = set(getattr(case, "teammate_player_guids", ()))
        self._focus_actor_roster = {
            actor["player_guid"]: {
                "class": actor["class"],
                "spec_key": actor["spec_key"],
                "teammate_runtime_actor": actor["player_guid"] in teammates,
            }
            for actor in actors if actor["player_guid"] in FOCUS_ACTORS
        }
        self._initial_deadlines: dict[str, dict[str, Any]] = {}

    def load_dynamic_v4(self, request: Mapping[str, Any], seed: int, config: Any) -> Any:
        """Capture existing initial deadline receipts before the first ready drain."""

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
        adapter = self.session.adapter
        self._initial_deadlines = {
            actor: {
                "planned_deadline": dict(adapter._deadline_by_actor[actor])
                if actor in adapter._deadline_by_actor else None,
                "horizon_discard": dict(adapter._horizon_discard_by_actor[actor])
                if actor in adapter._horizon_discard_by_actor else None,
                "active_armed": actor == adapter._active_actor,
                "armed_wake": dict(adapter._armed_by_actor[actor])
                if actor in adapter._armed_by_actor else None,
            }
            for actor in FOCUS_ACTORS
        }
        state = self._drain_ready(self._bridge.state())
        return replace(self.session.load_result, state=dict(state))

    def _record_target_event(self, emitted: Mapping[str, Any]) -> None:
        super()._record_target_event(emitted)
        receipt = emitted["wire_receipt"]
        sampled = emitted["sampled_emission"]
        actor = receipt["actor_guid"]
        if (
            actor in self._focus_actor_wake_marks
            and receipt["time_ms"] <= TARGET_DIAGNOSTIC_CUTOFF_MS
        ):
            self._focus_actor_wake_marks[actor].append({
                "time_ms": receipt["time_ms"],
                "sequence": emitted["scheduler_order_key"][2],
                "event_type": receipt["event_type"],
                "spell_id": sampled.get("spell_id"),
                "attribution_kind": sampled.get("attribution_kind"),
                "status": receipt["status"],
                "attackable_alive_target_count": len(
                    emitted.get("bridge_attackable_alive_target_indices_before_emission", ())
                ),
            })
        if (
            receipt["time_ms"] > TARGET_DIAGNOSTIC_CUTOFF_MS
            or receipt["event_type"] != "DMG"
            or sampled.get("spell_id") != 6603
            or sampled.get("attribution_kind") != "DIRECT_FRIENDLY_PLAYER"
        ):
            return
        transition = emitted["local_runtime_transition"]
        self._early_direct_white6603.append({
            "actor_guid": receipt["actor_guid"],
            "time_ms": receipt["time_ms"],
            "sequence": emitted["scheduler_order_key"][2],
            "status": receipt["status"],
            "target_index": receipt["target_index"],
            "target_guid": transition["target_guid"],
            "requested_damage": transition["requested_damage"],
            "applied_damage": receipt["applied_damage"],
            "target_selection_basis": emitted["target_selection_basis"],
        })

    def target_event_diagnostic(self) -> dict[str, Any]:
        diagnostic = super().target_event_diagnostic()
        diagnostic["early_direct_white6603_actor_trace"] = {
            "schema": "development_early_direct_white6603_actor_trace/v1",
            "cutoff_ms_inclusive": TARGET_DIAGNOSTIC_CUTOFF_MS,
            "events": sorted(
                self._early_direct_white6603,
                key=lambda row: (row["time_ms"], row["actor_guid"], row["sequence"]),
            ),
        }
        diagnostic["focus_actor_wake_marks"] = [
            {
                "actor_guid": actor,
                "runtime_roster": self._focus_actor_roster.get(actor),
                "drained_wake_count": len(rows),
                "first_wake_ms": rows[0]["time_ms"] if rows else None,
                "last_wake_ms": rows[-1]["time_ms"] if rows else None,
                "white6603_mark_count": sum(row["spell_id"] == 6603 for row in rows),
                "direct_white6603_mark_count": sum(
                    row["spell_id"] == 6603
                    and row["attribution_kind"] == "DIRECT_FRIENDLY_PLAYER"
                    for row in rows
                ),
                "wakes_with_attackable_target_count": sum(
                    row["attackable_alive_target_count"] > 0 for row in rows
                ),
                "marks": rows,
            }
            for actor, rows in self._focus_actor_wake_marks.items()
        ]
        if self.session is not None:
            adapter = self.session.adapter
            diagnostic["focus_actor_deadline_audit"] = [
                {
                    "actor_guid": actor,
                    "initial": self._initial_deadlines[actor],
                    "final_event_sequence": adapter._sequence_by_actor[actor],
                    "final_planned_deadline": dict(adapter._deadline_by_actor[actor])
                    if actor in adapter._deadline_by_actor else None,
                    "final_horizon_discard": dict(adapter._horizon_discard_by_actor[actor])
                    if actor in adapter._horizon_discard_by_actor else None,
                    "final_active_armed": actor == adapter._active_actor,
                    "final_armed_wake": dict(adapter._armed_by_actor[actor])
                    if actor in adapter._armed_by_actor else None,
                }
                for actor in FOCUS_ACTORS
            ]
        return diagnostic


__all__ = ["White6603ActorTraceDrivenBridgeV1"]
