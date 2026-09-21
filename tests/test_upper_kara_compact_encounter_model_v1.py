from __future__ import annotations

from copy import deepcopy

import pytest

from o2o_dps.chronicle_external_compact_target_reducer_v1 import SCHEMA as REDUCTION_SCHEMA
from o2o_dps.upper_kara_compact_encounter_model_v1 import (
    STATUS,
    UpperKaraCompactEncounterModelV1Error,
    build_compact_encounter_development_model_v1,
    target_bindings_from_compact_encounter_model_v1,
)
from o2o_dps.upper_kara_target_universe_resolution_v1 import (
    BOSS_OWNED_SUMMON_OCCURRENCE,
    EXCLUDED_EXACT_PLAYER_OWNED_UNIT,
    REGISTRY_CREATURE_OCCURRENCE,
    SCHEMA as RESOLUTION_SCHEMA,
    STATUS as RESOLUTION_STATUS,
)


FOCAL = "0x0000000000000001"
TEAMMATE = "0x0000000000000002"
GUID = "0xF130000000000001"
F130_EXTRA = "0xF13000EA4E000099"
F140_EXTRA = "0xF140008176000001"
PLAYER_OWNED = "0xF130002E5327A9D7"


def _encounter() -> dict:
    return {
        "instance_id": "raid-a",
        "encounter_id": "enc-a",
        "pull_ref": "raid-a:enc-a",
        "targets": [
            {
                "occurrence_id": "raid-a:enc-a:mob-a",
                "target_guid": GUID,
                "creature_entry_id": 123,
                "death_offset_ms": 1_000,
                "periods": [
                    {"start_offset_ms": 100, "end_offset_ms": 1_000}
                ],
            }
        ],
    }


def _reduction_target(
    guid: str,
    *,
    death_offset_ms: int = 1_000,
    activity_start_ms: int = 100,
    activity_end_ms: int = 900,
) -> dict:
    return {
        "target_guid": guid,
        "activity": {
            "status": "OBSERVED",
            "first_relevant_offset_ms": activity_start_ms,
            "last_relevant_offset_ms": activity_end_ms,
        },
        "incoming_damage": {
            "positive_sum": 100,
            "scope": "STRICTLY_BEFORE_FIRST_DEAD_MARKER",
            "amount_unavailable_event_count": 0,
            "by_player_guid": {FOCAL: 30, TEAMMATE: 60},
            "unattributed_positive_sum": 10,
            "overkill_adjusted_effective_damage": {
                "value": 100,
                "status": "FIELD_DERIVATION",
            },
        },
        "healing_received": {
            "positive_sum": 10,
            "amount_unavailable_event_count": 0,
            "scope": "STRICTLY_BEFORE_FIRST_DEAD_MARKER",
        },
        "post_first_death_activity": {
            "incoming_damage": {
                "positive_sum": 7,
                "positive_event_count": 1,
                "amount_unavailable_event_count": 0,
            },
            "healing_received": {
                "positive_sum": 0,
                "positive_event_count": 0,
                "amount_unavailable_event_count": 0,
            },
            "excluded_from_hp_balance_and_team_kill_clock": True,
        },
        "death": {"status": "OBSERVED", "offset_ms": death_offset_ms},
        "team_damage_timing": {
            "positive_incoming_damage_bins": [
                {
                    "start_offset_ms": activity_start_ms,
                    "end_offset_ms_exclusive": death_offset_ms + 100,
                    "positive_damage_sum": 100,
                    "positive_damage_by_player_guid": {
                        FOCAL: 30,
                        TEAMMATE: 60,
                    },
                }
            ]
        },
    }


def _reduction(*extra_targets: dict) -> dict:
    return {
        "schema": REDUCTION_SCHEMA,
        "status": "DESCRIPTIVE_OUTCOME_ONLY",
        "source_selection": {
            "instance_id": "raid-a",
            "encounter_id": "enc-a",
            "wave_id": "enc-a:wave:1",
        },
        "fury_focal_candidates": [
            {
                "player_guid": FOCAL,
                "player_class": "WARRIOR",
                "observed_spec": "Fury",
                "source_summary_closes_to_leave_one_out": True,
                "comparison_authorized": False,
            }
        ],
        "hostile_targets": [_reduction_target(GUID), *extra_targets],
    }


def _resolved_target(
    guid: str,
    occurrence_id: str,
    identity_kind: str,
    *,
    eligible: bool,
    creature_entry_id: int | None,
) -> dict:
    return {
        "target_guid": guid,
        "resolved_occurrence_id": occurrence_id,
        "resolution": identity_kind,
        "eligible_encounter_hostile": eligible,
        "stable_template_creature_entry_id": creature_entry_id,
    }


def _resolution(*extra_targets: dict, authorized: bool = True) -> dict:
    return {
        "schema": RESOLUTION_SCHEMA,
        "status": RESOLUTION_STATUS,
        "source": {
            "instance_id": "raid-a",
            "encounter_id": "enc-a",
            "pull_ref": "raid-a:enc-a",
        },
        "targets": [
            _resolved_target(
                GUID,
                "raid-a:enc-a:mob-a",
                REGISTRY_CREATURE_OCCURRENCE,
                eligible=True,
                creature_entry_id=123,
            ),
            *extra_targets,
        ],
        "gate": {"development_case_assembly_authorized": authorized},
    }


def test_builds_hp_hypothesis_armor_grid_and_focal_leave_one_out() -> None:
    model = build_compact_encounter_development_model_v1(
        _encounter(),
        _reduction(),
        _resolution(),
        model_id="incantagos/attempt-2/development-v1",
        focal_player_guid=FOCAL,
        armor_hypotheses=(4_211, 1_721, 2_861, 1_961, 3_761),
    )
    target = model["targets"][0]

    assert model["status"] == STATUS
    assert target["hp_model"]["point_health"] == 90
    assert target["hp_model"]["observed_max_health"] is None
    assert target["armor_model"]["base_armor_hypotheses"] == [
        1_721,
        1_961,
        2_861,
        3_761,
        4_211,
    ]
    team = target["team_kill_clock_model"]
    assert team["focal_positive_damage_removed"] == 30
    assert team["leave_one_out_positive_damage"] == 70
    assert team["teammate_positive_damage_by_player_guid"] == {TEAMMATE: 60}
    assert team["residual_positive_damage_without_exact_player_guid"] == 10
    assert team["explicit_unattributed_positive_damage"] == 10
    assert team["damage_bins"][0]["leave_one_out_positive_damage"] == 70
    assert team["damage_bins"][0][
        "residual_positive_damage_without_exact_player_guid"
    ] == 10
    assert team["responsive_to_candidate_policy"] is False
    assert model["scientific_boundaries"]["comparison_authorized"] is False

    bindings = target_bindings_from_compact_encounter_model_v1(model)
    binding = bindings["raid-a:enc-a:mob-a"]
    assert binding.target_guid == GUID
    assert binding.identity_kind == REGISTRY_CREATURE_OCCURRENCE
    assert binding.creature_entry_id == 123
    assert binding.hp_model_ref.endswith("/hp")
    assert binding.team_kill_clock_ref.endswith("/team-kill-clock")


def test_models_registry_f130_and_f140_but_excludes_exact_player_owned() -> None:
    f130_occurrence = f"raid-a:enc-a:{F130_EXTRA}"
    f140_occurrence = f"raid-a:enc-a:{F140_EXTRA}"
    player_occurrence = f"raid-a:enc-a:{PLAYER_OWNED}"
    resolution = _resolution(
        _resolved_target(
            F130_EXTRA,
            f130_occurrence,
            BOSS_OWNED_SUMMON_OCCURRENCE,
            eligible=True,
            creature_entry_id=59_982,
        ),
        _resolved_target(
            F140_EXTRA,
            f140_occurrence,
            BOSS_OWNED_SUMMON_OCCURRENCE,
            eligible=True,
            creature_entry_id=None,
        ),
        _resolved_target(
            PLAYER_OWNED,
            player_occurrence,
            EXCLUDED_EXACT_PLAYER_OWNED_UNIT,
            eligible=False,
            creature_entry_id=None,
        ),
    )
    reduction = _reduction(
        _reduction_target(
            F130_EXTRA,
            death_offset_ms=1_200,
            activity_start_ms=200,
            activity_end_ms=1_100,
        ),
        _reduction_target(
            F140_EXTRA,
            death_offset_ms=1_300,
            activity_start_ms=300,
            activity_end_ms=1_250,
        ),
        _reduction_target(PLAYER_OWNED, death_offset_ms=800),
    )

    model = build_compact_encounter_development_model_v1(
        _encounter(),
        reduction,
        resolution,
        model_id="model",
        focal_player_guid=FOCAL,
        armor_hypotheses=(1_721,),
    )

    by_guid = {row["target_guid"]: row for row in model["targets"]}
    assert set(by_guid) == {GUID, F130_EXTRA, F140_EXTRA}
    assert PLAYER_OWNED not in by_guid
    assert by_guid[F130_EXTRA]["identity_kind"] == BOSS_OWNED_SUMMON_OCCURRENCE
    assert by_guid[F130_EXTRA]["creature_entry_id"] == 59_982
    f140 = by_guid[F140_EXTRA]
    assert f140["identity_kind"] == BOSS_OWNED_SUMMON_OCCURRENCE
    assert f140["creature_entry_id"] is None
    assert f140["attackability_model"]["activity_windows"] == [
        {"start_offset_ms": 300, "end_offset_ms": 1_250}
    ]
    assert f140["attackability_model"]["kind"] == (
        "EXTERNAL_REDUCTION_DESCRIPTIVE_OUTCOME_PROXY"
    )
    assert f140["team_kill_clock_model"]["death_offset_source"] == (
        "EXTERNAL_REDUCTION_DESCRIPTIVE_OUTCOME"
    )
    assert by_guid[GUID]["attackability_model"]["kind"] == (
        "REGISTRY_METADATA_PERIOD_PROXY"
    )
    boundaries = model["scientific_boundaries"]
    assert boundaries["target_universe_matches_registry_exactly"] is False
    assert boundaries["eligible_target_count"] == 3
    assert boundaries["excluded_exact_player_owned_target_count"] == 1
    assert boundaries["comparison_authorized"] is False

    bindings = target_bindings_from_compact_encounter_model_v1(model)
    assert bindings[f140_occurrence].creature_entry_id is None
    assert bindings[f140_occurrence].target_guid == F140_EXTRA


def test_resolution_must_be_authorized_and_f140_cannot_invent_template_entry() -> None:
    with pytest.raises(
        UpperKaraCompactEncounterModelV1Error,
        match="not authorized",
    ):
        build_compact_encounter_development_model_v1(
            _encounter(),
            _reduction(),
            _resolution(authorized=False),
            model_id="model",
            focal_player_guid=FOCAL,
            armor_hypotheses=(1_721,),
        )

    f140_occurrence = f"raid-a:enc-a:{F140_EXTRA}"
    bad_resolution = _resolution(
        _resolved_target(
            F140_EXTRA,
            f140_occurrence,
            BOSS_OWNED_SUMMON_OCCURRENCE,
            eligible=True,
            creature_entry_id=33_142,
        )
    )
    with pytest.raises(
        UpperKaraCompactEncounterModelV1Error,
        match="must not expose a template entry",
    ):
        build_compact_encounter_development_model_v1(
            _encounter(),
            _reduction(_reduction_target(F140_EXTRA)),
            bad_resolution,
            model_id="model",
            focal_player_guid=FOCAL,
            armor_hypotheses=(1_721,),
        )


def test_mismatched_death_or_unclosed_bins_fail_closed() -> None:
    reduction = _reduction()
    reduction["hostile_targets"][0]["death"]["offset_ms"] = 999
    with pytest.raises(UpperKaraCompactEncounterModelV1Error, match="death offset"):
        build_compact_encounter_development_model_v1(
            _encounter(),
            reduction,
            _resolution(),
            model_id="model",
            focal_player_guid=FOCAL,
            armor_hypotheses=(1_721,),
        )

    reduction = _reduction()
    reduction["hostile_targets"][0]["team_damage_timing"][
        "positive_incoming_damage_bins"
    ][0]["positive_damage_sum"] = 99
    with pytest.raises(UpperKaraCompactEncounterModelV1Error, match="do not close"):
        build_compact_encounter_development_model_v1(
            _encounter(),
            reduction,
            _resolution(),
            model_id="model",
            focal_player_guid=FOCAL,
            armor_hypotheses=(1_721,),
        )


def test_missing_target_evidence_and_incomplete_damage_are_rejected() -> None:
    reduction = _reduction()
    reduction["hostile_targets"] = []
    with pytest.raises(UpperKaraCompactEncounterModelV1Error, match="target universe"):
        build_compact_encounter_development_model_v1(
            _encounter(),
            reduction,
            _resolution(),
            model_id="model",
            focal_player_guid=FOCAL,
            armor_hypotheses=(1_721,),
        )


def test_rejects_unobserved_focal_and_extra_hostile_target() -> None:
    with pytest.raises(
        UpperKaraCompactEncounterModelV1Error,
        match="observed exact-GUID Fury",
    ):
        build_compact_encounter_development_model_v1(
            _encounter(),
            _reduction(),
            _resolution(),
            model_id="model",
            focal_player_guid="0x0000000000000099",
            armor_hypotheses=(1_721,),
        )

    reduction = _reduction()
    extra = deepcopy(reduction["hostile_targets"][0])
    extra["target_guid"] = "0xF130000000000099"
    reduction["hostile_targets"].append(extra)
    with pytest.raises(UpperKaraCompactEncounterModelV1Error, match="target universe"):
        build_compact_encounter_development_model_v1(
            _encounter(),
            reduction,
            _resolution(),
            model_id="model",
            focal_player_guid=FOCAL,
            armor_hypotheses=(1_721,),
        )


def test_named_player_damage_must_not_exceed_target_or_bin_total() -> None:
    reduction = _reduction()
    reduction["hostile_targets"][0]["incoming_damage"]["by_player_guid"][
        TEAMMATE
    ] = 80
    with pytest.raises(
        UpperKaraCompactEncounterModelV1Error,
        match="named player damage exceeds total",
    ):
        build_compact_encounter_development_model_v1(
            _encounter(),
            reduction,
            _resolution(),
            model_id="model",
            focal_player_guid=FOCAL,
            armor_hypotheses=(1_721,),
        )

    reduction = _reduction()
    reduction["hostile_targets"][0]["incoming_damage"][
        "overkill_adjusted_effective_damage"
    ] = {"value": None, "status": "UNAVAILABLE"}
    with pytest.raises(UpperKaraCompactEncounterModelV1Error, match="lacks complete"):
        build_compact_encounter_development_model_v1(
            _encounter(),
            reduction,
            _resolution(),
            model_id="model",
            focal_player_guid=FOCAL,
            armor_hypotheses=(1_721,),
        )


def test_model_bindings_reject_mutated_status() -> None:
    model = build_compact_encounter_development_model_v1(
        _encounter(),
        _reduction(),
        _resolution(),
        model_id="model",
        focal_player_guid=FOCAL,
        armor_hypotheses=(1_721,),
    )
    mutated = deepcopy(model)
    mutated["status"] = "COMPARISON_READY"
    with pytest.raises(UpperKaraCompactEncounterModelV1Error, match="schema or status"):
        target_bindings_from_compact_encounter_model_v1(mutated)
