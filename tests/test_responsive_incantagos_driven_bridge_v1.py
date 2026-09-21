from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import o2o_dps.responsive_incantagos_driven_bridge_v1 as module


@dataclass(frozen=True)
class _Load:
    state: dict


class _Adapter:
    def __init__(self, bridge):
        self.bridge = bridge
        self.calls = 0

    def emit_global_ready_and_rearm(self):
        self.calls += 1
        self.bridge.ready = False
        return {"emitted": _emitted(
            actor="actor-a", time_ms=self.bridge.time_ms,
            target=0, event_type="DMG", spell_id=6603, damage=7,
        )}


def _emitted(*, actor, time_ms, target, event_type, spell_id, damage,
             head=None, status="APPLIED", target_mode="STAY_ALIVE"):
    return {
        "wire_receipt": {
            "actor_guid": actor, "time_ms": time_ms, "target_index": target,
            "event_type": event_type, "status": status,
            "applied_damage": damage,
        },
        "sampled_emission": {"spell_id": spell_id, "target_mode": target_mode},
        "target_selection_basis": (
            "LEARNED_DIRECT_START_PREFIX_TARGET_CHOICE" if head else
            "ATTACKABLE_ALIVE_REGISTRY_UNCALIBRATED_DAMAGE_TARGET"
        ),
        "target_choice_head": head,
    }


class _RawBridge:
    def __init__(self):
        self.ready = True
        self.time_ms = 0
        self.advance_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def state(self):
        return {
            "time_ms": self.time_ms,
            "needs_input": True,
            "wake_ready": {"wake_id": "due"} if self.ready else None,
        }

    def advance(self):
        self.advance_calls += 1
        self.time_ms = 100
        self.ready = True
        return self.state()


def test_ready_response_is_drained_on_load_and_advance(monkeypatch):
    raw = _RawBridge()
    adapter = _Adapter(raw)
    calls = []

    def bind(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(load_result=_Load({"time_ms": 0}), adapter=adapter)

    monkeypatch.setattr(module, "bind_responsive_incantagos_development_session_v1", bind)
    case = SimpleNamespace(request={"same": "request"}, dynamic_config="same-config")
    with module.IncantagosDevelopmentDrivenBridgeV1(
        bridge=raw, case=case, loaded_model=object(), teammate_seed=32
    ) as driven:
        load = driven.load_dynamic_v4({"same": "request"}, 41, "same-config")
        assert load.state["wake_ready"] is None
        assert driven.responsive_event_count == 1
        state = driven.advance()
        assert state["wake_ready"] is None
        assert driven.responsive_event_count == 2
        assert driven.responsive_applied_damage == 14
        diagnostic = driven.target_event_diagnostic()
        assert diagnostic["first_white_6603_actor_count"] == 1
        assert diagnostic["groups"][0]["event_count"] == 2
        assert diagnostic["groups"][0]["applied_damage"] == 14
    assert raw.advance_calls == 1
    assert calls[0]["simulator_seed"] == 41
    assert calls[0]["teammate_seed"] == 32


def test_target_diagnostic_buckets_first_white_and_start_head_without_event_trace():
    driven = module.IncantagosDevelopmentDrivenBridgeV1(
        bridge=_RawBridge(), case=object(), loaded_model=object(), teammate_seed=32
    )
    events = [
        _emitted(actor="actor-a", time_ms=1000, target=0,
                 event_type="DMG", spell_id=6603, damage=7),
        _emitted(actor="actor-a", time_ms=10000, target=1,
                 event_type="DMG", spell_id=6603, damage=4),
        _emitted(actor="actor-b", time_ms=10000, target=1,
                 event_type="DMG", spell_id=6603, damage=3),
        _emitted(actor="actor-a", time_ms=10001, target=1,
                 event_type="START", spell_id=25286, damage=0,
                 head={"target_guid": "target-1"}, status="OBSERVED_NO_DAMAGE"),
        _emitted(actor="actor-b", time_ms=10002, target=2,
                 event_type="START", spell_id=25286, damage=0,
                 status="OBSERVED_NO_DAMAGE"),
        _emitted(actor="actor-b", time_ms=10003, target=None,
                 event_type="START", spell_id=25289, damage=0,
                 target_mode="NO_TARGET", status="OBSERVED_NO_DAMAGE"),
    ]
    for emitted in events:
        driven._record_target_event(emitted)
    diagnostic = driven.target_event_diagnostic()
    assert sum(row["applied_damage"] for row in diagnostic["groups"]) == 14
    assert sum(row["applied_hit_count"] for row in diagnostic["groups"]) == 3
    assert diagnostic["first_white_6603_actor_count"] == 2
    assert {(row["time_bucket"], row["target_index"], row["actor_count"])
            for row in diagnostic["first_white_6603_groups"]} == {
                ("through_9098ms", 0, 1), ("after_9098ms", 1, 1),
            }
    assert diagnostic["direct_start_head_counts"] == {
        "head": 1, "without_head": 1, "nonhostile_or_untargeted": 1,
    }


def test_load_rejects_a_different_case_before_binding(monkeypatch):
    raw = _RawBridge()
    monkeypatch.setattr(
        module,
        "bind_responsive_incantagos_development_session_v1",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not bind")),
    )
    case = SimpleNamespace(request={"same": "request"}, dynamic_config="same-config")
    driven = module.IncantagosDevelopmentDrivenBridgeV1(
        bridge=raw, case=case, loaded_model=object(), teammate_seed=32
    )
    try:
        driven.load_dynamic_v4({"different": "request"}, 41, "same-config")
    except ValueError as error:
        assert "differs" in str(error)
    else:
        raise AssertionError("different request must be refused")
