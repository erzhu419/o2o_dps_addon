from __future__ import annotations

import json
from types import SimpleNamespace

from scripts import development_d900_v7_white6603_conditional_audit_v1 as audit


def test_actionable_projection_counts_only_direct_hostile_6603(monkeypatch):
    direct_white = json.dumps(["DMG", 6603, "DIRECT_FRIENDLY_PLAYER"])
    indirect_white = json.dumps(["DMG", 6603, "OWNER_FRIENDLY_PLAYER"])
    other = json.dumps(["GO", 23894, "DIRECT_FRIENDLY_PLAYER"])
    projection = SimpleNamespace(
        mark_support=10, actionable_support=10,
        choices=(
            ((direct_white, "STAY_ALIVE", 4, 5), 3),
            ((direct_white, "SWITCH_ALIVE", 0, 5), 1),
            ((direct_white, "NO_TARGET", 4, 1), 1),
            ((indirect_white, "STAY_ALIVE", 4, 1), 1),
            ((other, "STAY_ALIVE", 0, 4), 4),
        ),
    )
    monkeypatch.setattr(audit.response, "_context_keys", lambda *_: [("GLOBAL",)])
    model = SimpleNamespace(
        variant_id="D", minimums={"GLOBAL": 1},
        _actionable_projection=lambda context: projection,
    )
    p_white, p_positive, level = audit._white_mark_probabilities(model, {}, {})
    assert p_white == 0.4
    assert p_positive == 0.3
    assert level == "GLOBAL"


def test_actor_and_wave_first_white_hazard_use_same_historical_opportunities():
    rows = [
        {
            "actor_guid": "a", "time_ms": 100,
            "p_direct_hostile_white6603": 0.5,
            "p_direct_hostile_positive_raw_white6603": 0.4,
            "observed_direct_hostile_white6603": False,
            "observed_positive_raw_white6603": False,
            "mark_context_level": "CLASS_SPEC", "delay_context_level": "GLOBAL",
            "delay_origin": "WAVE_START", "delay_bucket_nll": 1.0,
        },
        {
            "actor_guid": "a", "time_ms": 200,
            "p_direct_hostile_white6603": 0.25,
            "p_direct_hostile_positive_raw_white6603": 0.1,
            "observed_direct_hostile_white6603": True,
            "observed_positive_raw_white6603": True,
            "mark_context_level": "CLASS_SPEC", "delay_context_level": "CLASS_SPEC",
            "delay_origin": "PREVIOUS_ACTOR_EVENT", "delay_bucket_nll": 2.0,
        },
    ]
    result = audit._aggregate(rows, cutoff_ms=None)
    assert result["event_opportunities"] == 2
    assert result["observed_direct_white6603_emitted_raw"] == 1
    assert result["expected_direct_white6603_mark_at_observed_opportunities"] == 0.75
    assert result["expected_direct_white6603_positive_raw_mark_at_observed_opportunities"] == 0.5
    assert result["observed_first_direct_white6603_ms"] == 200
    assert result["p_any_first_white6603_at_observed_opportunities"] == 0.625
    assert result["expected_first_white6603_ms_conditional_on_any_at_observed_opportunities"] == 120
    assert result["delay_bucket_score"]["first_any_mean_nll_finite"] == 1.0
    assert result["delay_bucket_score"]["after_event_mean_nll_finite"] == 2.0
