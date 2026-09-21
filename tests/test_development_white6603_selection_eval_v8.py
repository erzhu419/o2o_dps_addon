from collections import Counter
import json
import math
from types import SimpleNamespace

import pytest

from o2o_dps.development_white6603_joint_training_v8 import WHITE_TOKEN
from o2o_dps.development_white6603_selection_eval_v8 import (
    evaluate_selection_v8, evaluate_selection_partials_v8,
    score_opportunity_v8, score_selection_partial_v8,
)


IDS = [f"raid-{index:02d}" for index in range(13)]


def _scored_rows(*, p7=0.1, p_half=0.3, p_one=0.25):
    for instance_id in IDS:
        for time_ms, white in ((1000, False), (2000, True), (12000, False)):
            yield {
                "instance_id": instance_id, "wave_id": "wave-1",
                "time_ms": time_ms, "actor_class": "WARRIOR",
                "actor_spec": "WARRIOR_FURY", "observed_white": white,
                "p_white": {"v7_raw": p7, "beta_half": p_half, "beta_one": p_one},
                "p_nonwhite_token_given_nonwhite": None if white else 0.5,
            }


def test_selection_gate_and_first_hit_censoring():
    result = evaluate_selection_v8(_scored_rows(), IDS)
    assert result["status"] == "PASS_INTRA_COMPONENT_DEVELOPMENT_GATE"
    assert result["selected_candidate"] == "beta_half"
    assert (result["opportunity_count"], result["wave_count"]) == (39, 13)
    assert (result["observed_white"], result["observed_early_white"]) == (13, 13)
    # The third opportunity is after observed first white and is omitted only
    # from the first-hit hazard, not from the ordinary binary mark NLL.
    expected_first_nll = -math.log(0.7) - math.log(0.3)
    assert result["metrics"]["beta_half"]["first_white_hazard_mean_nll"] == pytest.approx(expected_first_nll)
    assert result["metrics"]["beta_half"]["binary_mean_nll"] == pytest.approx(
        (expected_first_nll - math.log(0.7)) / 3
    )
    assert result["metrics"]["beta_half"]["nonwhite_mark_mean_nll"] == result["metrics"]["v7_raw"]["nonwhite_mark_mean_nll"]
    assert "nonwhite_mark_nll_no_worse" not in result["gates"]
    assert "descriptive" in result["nonwhite_mark_invariant"]
    assert result["class_spec_descriptive_only"][0]["opportunities"] == 39


def test_parallel_raid_partials_reduce_to_serial_gate_independent_of_finish_order():
    rows = list(_scored_rows())
    rows[0]["p_white"]["v7_raw"] = 1.0  # Real zero-likelihood baseline is JSON-safe.
    serial = evaluate_selection_v8(rows, IDS)
    parts = [
        score_selection_partial_v8(
            (row for row in rows if row["instance_id"] in set(IDS[index::6])),
            IDS[index::6],
        )
        for index in range(6)
    ]
    parts = [json.loads(json.dumps(part, allow_nan=False)) for part in reversed(parts)]
    parallel = evaluate_selection_partials_v8(parts, IDS)
    assert parallel == serial
    assert parallel["metrics"]["v7_raw"]["binary_zero_likelihood"]


def test_parallel_reducer_rejects_duplicate_or_missing_raid():
    one = score_selection_partial_v8(
        (row for row in _scored_rows() if row["instance_id"] == IDS[0]), [IDS[0]]
    )
    with pytest.raises(ValueError, match="overlap"):
        evaluate_selection_partials_v8([one, one], IDS)
    with pytest.raises(ValueError, match="coverage"):
        evaluate_selection_partials_v8([one], IDS)


def test_no_candidate_passes_when_white_fit_worsens():
    result = evaluate_selection_v8(_scored_rows(p_half=0.01, p_one=0.02), IDS)
    assert result["status"] == "FAIL_INTRA_COMPONENT_DEVELOPMENT_GATE"
    assert not result["gates"]["binary_white_nll_improved"]


def test_wave_count_error_does_not_cancel_across_raids():
    rows = []
    for index, instance_id in enumerate(IDS):
        observed = index == 0
        p7 = 0.1 if index == 0 else 0.9 if index == 1 else 0.5
        p8 = 0.6 if index == 0 else 0.4 if index == 1 else 0.5
        rows.append({
            "instance_id": instance_id, "wave_id": "wave-1", "time_ms": 1000,
            "actor_class": "WARRIOR", "actor_spec": "WARRIOR_FURY",
            "observed_white": observed,
            "p_white": {"v7_raw": p7, "beta_half": p8, "beta_one": p8},
            "p_nonwhite_token_given_nonwhite": None if observed else 0.5,
        })
    metrics = evaluate_selection_v8(rows, IDS)["metrics"]
    assert metrics["v7_raw"]["absolute_white_count_bias"] == pytest.approx(
        metrics["beta_half"]["absolute_white_count_bias"]
    )
    assert (metrics["beta_half"]["mean_wave_absolute_white_count_error"]
            < metrics["v7_raw"]["mean_wave_absolute_white_count_error"])


def test_incomplete_selection_and_external_raid_are_rejected():
    with pytest.raises(ValueError, match="coverage"):
        evaluate_selection_v8((row for row in _scored_rows() if row["instance_id"] != IDS[-1]), IDS)
    with pytest.raises(ValueError, match="outside"):
        evaluate_selection_v8([{"instance_id": "d900"}], IDS)


def test_fit_only_counts_score_label_without_using_it_as_feature():
    nonwhite = '["GO",23881,"DIRECT_FRIENDLY_PLAYER"]'
    model = SimpleNamespace(
        mark_counts={("GLOBAL",): Counter({WHITE_TOKEN: 1, nonwhite: 3})},
        minimums={"GUID": 25, "CLASS_SPEC": 50, "CLASS": 100, "GLOBAL": 1},
    )
    row = {
        "actor": {"player_guid": "actor-a", "class": "WARRIOR", "spec_key": "WARRIOR_FURY"},
        "emission_state_before_current_event": {
            "marked_activity": {"other_team_including_unattributed": {
                "action_event_count_3000ms": 0, "damage_amount_3000ms": 0,
            }},
            "target_state": {"alive_target_count": 1},
            "actor_last_mark_token": None,
            "white6603_phase": "FIRST", "wave_elapsed_ms": 1000,
            "white6603_age_ms": 1000,
            "actor_has_prior_direct_hostile_start": False,
        },
        "label": {"mark_token": nonwhite, "attribution_kind": "DIRECT_FRIENDLY_PLAYER",
                  "event_type": "GO", "spell_id": 23881},
    }
    counts = {("GLOBAL", "FIRST"): Counter({True: 4, False: 6})}
    scored = score_opportunity_v8(
        row=row, v7_fit_model=model, v8_fit_white_counts=counts,
        instance_id=IDS[0], wave_id="wave-1",
    )
    assert scored["p_white"]["v7_raw"] == 0.25
    assert scored["p_white"]["beta_half"] == pytest.approx(4.5 / 11)
    assert scored["p_white"]["beta_one"] == pytest.approx(5 / 12)
    assert scored["p_nonwhite_token_given_nonwhite"] == pytest.approx(3.5 / 4)
