from __future__ import annotations

import json

import pytest

from o2o_dps.upper_kara_cat_action_plan_append_contract_v9 import (
    FrozenV8ParentRefV9,
)
from o2o_dps.upper_kara_cat_action_plan_append_selection_v9 import (
    PairedParentDamageV9,
    paired_parent_damage_from_dict_v9,
    paired_parent_damage_rows_from_remote_lanes_v9,
    select_paired_parent_append_v9,
)


PARENT = "selected-v8-parent"
CHALLENGER = "parent-plus-one"
LOADOUT = "mighty_rage"


def _parent() -> FrozenV8ParentRefV9:
    return FrozenV8ParentRefV9(
        source_campaign_id="v8",
        source_freeze_ref="v8/frozen.json",
        build_id="build-a",
        loadout_id=LOADOUT,
        program_ref=PARENT,
        program_id=PARENT,
        program_key="parent-program-key",
        policy_bundle_ref="v8/mighty_rage.policy.json",
    )


def _rows(
    cohort: str,
    *,
    first_seed: int,
    count: int,
    challenger_delta: float,
    include_challenger: bool = True,
) -> tuple[PairedParentDamageV9, ...]:
    rows: list[PairedParentDamageV9] = []
    refs = (PARENT, CHALLENGER) if include_challenger else (PARENT,)
    for ref in refs:
        for offset in range(count):
            parent_damage = 10_000.0 + offset
            rows.append(
                PairedParentDamageV9(
                    cohort=cohort,
                    loadout_id=LOADOUT,
                    candidate_ref=ref,
                    paired_parent_ref=PARENT,
                    seed=first_seed + offset,
                    simulator_seed=900_000 + first_seed + offset,
                    candidate_damage=(
                        parent_damage
                        if ref == PARENT
                        else parent_damage + challenger_delta
                    ),
                    parent_damage=parent_damage,
                )
            )
    return tuple(rows)


def test_row_roundtrip_and_exact_parent_delta() -> None:
    row = _rows(
        "PROPOSAL", first_seed=1, count=1, challenger_delta=4.0
    )[0]
    assert paired_parent_damage_from_dict_v9(row.to_dict()) == row
    assert row.damage_delta == 0.0

    with pytest.raises(ValueError, match="exact delta=0"):
        PairedParentDamageV9(
            cohort="PROPOSAL",
            loadout_id=LOADOUT,
            candidate_ref=PARENT,
            paired_parent_ref=PARENT,
            seed=1,
            simulator_seed=2,
            candidate_damage=11,
            parent_damage=10,
        )


def test_positive_selection_lcb_accepts_append() -> None:
    result = select_paired_parent_append_v9(
        proposal_rows=_rows(
            "PROPOSAL", first_seed=1_420_001, count=128, challenger_delta=8
        ),
        selection_rows=_rows(
            "SELECTION", first_seed=1_420_129, count=128, challenger_delta=3
        ),
    )

    assert result["status"] == "APPEND_ACCEPTED_SELECTION_LCB_POSITIVE"
    assert result["proposal_ranked_candidate"]["candidate_ref"] == CHALLENGER
    assert result["accepted_program"] == {
        "program_ref": CHALLENGER,
        "is_parent_unchanged": False,
    }
    assert result["selection_interval"]["lower_bound"] == 3.0
    assert result["contract"]["heldout_outcomes_observed"] is False
    assert ("zero" + "_residual") not in json.dumps(result)


def test_nonpositive_selection_lcb_legally_retains_parent() -> None:
    result = select_paired_parent_append_v9(
        proposal_rows=_rows(
            "PROPOSAL", first_seed=10, count=64, challenger_delta=5
        ),
        selection_rows=_rows(
            "SELECTION", first_seed=1_000, count=64, challenger_delta=-1
        ),
    )

    assert result["status"] == "PARENT_RETAINED_SELECTION_LCB_NOT_POSITIVE"
    assert result["accepted_program"] == {
        "program_ref": PARENT,
        "is_parent_unchanged": True,
    }
    assert result["selection_interval"]["upper_bound"] == -1.0


def test_parent_wins_proposal_ties_without_running_a_challenger_gate() -> None:
    result = select_paired_parent_append_v9(
        proposal_rows=_rows(
            "PROPOSAL", first_seed=10, count=32, challenger_delta=0
        ),
        selection_rows=_rows(
            "SELECTION", first_seed=1_000, count=32, challenger_delta=100
        ),
    )

    assert result["status"] == "PARENT_RETAINED_ON_PROPOSAL_RANKING"
    assert result["proposal_ranked_candidate"]["candidate_ref"] == PARENT
    assert result["selection_interval"] is None


def test_insufficient_selection_pairs_retains_parent() -> None:
    result = select_paired_parent_append_v9(
        proposal_rows=_rows(
            "PROPOSAL", first_seed=10, count=32, challenger_delta=5
        ),
        selection_rows=_rows(
            "SELECTION", first_seed=1_000, count=8, challenger_delta=5
        ),
        minimum_selection_pairs=32,
    )

    assert result["status"] == "PARENT_RETAINED_INSUFFICIENT_SELECTION_PAIRS"
    assert result["accepted_program"]["is_parent_unchanged"] is True


def test_projection_from_remote_lanes_uses_same_simulator_seed_pairs() -> None:
    lanes = []
    for ref, damage in ((PARENT, 100.0), (CHALLENGER, 105.0)):
        lanes.append(
            {
                "program_ref": ref,
                "master_seed": 11,
                "simulator_seed": 99,
                "status": "COMPLETE",
                "own_effective_damage": damage,
            }
        )
    rows = paired_parent_damage_rows_from_remote_lanes_v9(
        cohort="PROPOSAL",
        parent=_parent(),
        candidate_refs=(PARENT, CHALLENGER),
        lanes=lanes,
    )
    assert len(rows) == 2
    assert rows[1].damage_delta == 5.0

    lanes[1]["simulator_seed"] = 100
    with pytest.raises(ValueError, match="different simulator seeds"):
        paired_parent_damage_rows_from_remote_lanes_v9(
            cohort="PROPOSAL",
            parent=_parent(),
            candidate_refs=(PARENT, CHALLENGER),
            lanes=lanes,
        )


def test_selection_rejects_seed_overlap_and_missing_ranked_candidate() -> None:
    proposal = _rows(
        "PROPOSAL", first_seed=10, count=32, challenger_delta=5
    )
    overlapping = _rows(
        "SELECTION", first_seed=10, count=32, challenger_delta=5
    )
    with pytest.raises(ValueError, match="overlap"):
        select_paired_parent_append_v9(
            proposal_rows=proposal,
            selection_rows=overlapping,
        )

    parent_only = _rows(
        "SELECTION",
        first_seed=1_000,
        count=32,
        challenger_delta=0,
        include_challenger=False,
    )
    with pytest.raises(ValueError, match="lacks"):
        select_paired_parent_append_v9(
            proposal_rows=proposal,
            selection_rows=parent_only,
        )


def test_selection_rejects_candidate_rows_bound_to_different_parent_damage() -> None:
    proposal = list(
        _rows("PROPOSAL", first_seed=10, count=32, challenger_delta=5)
    )
    challenger_index = next(
        index
        for index, row in enumerate(proposal)
        if row.candidate_ref == CHALLENGER
    )
    bad = proposal[challenger_index]
    proposal[challenger_index] = PairedParentDamageV9(
        **{**bad.to_dict(), "parent_damage": bad.parent_damage + 1}
    )
    with pytest.raises(ValueError, match="explicit parent damage"):
        select_paired_parent_append_v9(
            proposal_rows=proposal,
            selection_rows=_rows(
                "SELECTION", first_seed=1_000, count=32, challenger_delta=5
            ),
        )
