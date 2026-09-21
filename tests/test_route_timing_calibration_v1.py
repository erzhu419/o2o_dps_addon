from __future__ import annotations

from pathlib import Path
import sys

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.raid_cooldown_schedule_v1 import (
    TRASH,
    CooldownPackageV1,
    CooldownUseV1,
    EncounterCooldownCellV1,
    allocate_raid_cooldown_packages_v1,
)
from o2o_dps.route_timing_calibration_v1 import (
    PairedContinuousRouteTimingObservationV1,
    RouteEncounterStartV1,
    calibrate_route_timing_shift_profile_v1,
    persistent_route_time_shift_bounds_v1,
)


ROUTE = ("wave-a", "wave-b", "boss")
BASELINE = (0, 10_000, 30_000)


def _starts(times: tuple[int, ...], ids: tuple[str, ...] = ROUTE):
    return tuple(
        RouteEncounterStartV1(encounter_id, start_ms)
        for encounter_id, start_ms in zip(ids, times, strict=True)
    )


def _observation(
    seed: int,
    candidate: tuple[int, ...],
    *,
    source: str = "wave-a",
    package: str = "fast-a",
    baseline: tuple[int, ...] = BASELINE,
) -> PairedContinuousRouteTimingObservationV1:
    return PairedContinuousRouteTimingObservationV1(
        route_case_id="upper-kara/build-a/route-1",
        source_encounter_id=source,
        package_id=package,
        seed=seed,
        baseline_starts=_starts(baseline),
        candidate_starts=_starts(candidate),
    )


def test_calibration_preserves_each_seed_and_uses_extrema_not_average() -> None:
    profile = calibrate_route_timing_shift_profile_v1((
        _observation(19, (0, 9_200, 29_200)),
        _observation(7, (0, 9_000, 29_000)),
    ))

    assert profile.seeds == (7, 19)
    assert profile.downstream[0].seed_shifts_ms == ((7, -1_000), (19, -800))
    assert (
        profile.downstream[0].shift_ms_min,
        profile.downstream[0].shift_ms_max,
    ) == (-1_000, -800)
    assert profile.is_persistent_per_seed is True
    assert profile.persistent_shift_bounds() == (-1_000, -800)
    assert profile.to_dict()["downstream"][0]["paired_seed_count"] == 2


def test_door_anchor_absorption_is_recorded_per_edge_and_fails_legacy() -> None:
    profile = calibrate_route_timing_shift_profile_v1((
        _observation(7, (0, 9_000, 30_000)),
        _observation(19, (0, 9_200, 30_000)),
    ))

    assert [
        (row.encounter_id, row.shift_ms_min, row.shift_ms_max)
        for row in profile.downstream
    ] == [
        ("wave-b", -1_000, -800),
        ("boss", 0, 0),
    ]
    assert profile.is_persistent_per_seed is False
    with pytest.raises(ValueError, match="edge-dependent route timing"):
        profile.persistent_shift_bounds()
    with pytest.raises(ValueError, match="edge-dependent route timing"):
        persistent_route_time_shift_bounds_v1((profile,))


def test_candidate_must_share_the_route_and_pre_source_checkpoint() -> None:
    with pytest.raises(ValueError, match="identical ordered route"):
        PairedContinuousRouteTimingObservationV1(
            route_case_id="route",
            source_encounter_id="wave-a",
            package_id="candidate",
            seed=1,
            baseline_starts=_starts(BASELINE),
            candidate_starts=_starts((0, 9_000, 29_000), ("wave-a", "boss", "wave-b")),
        )

    with pytest.raises(ValueError, match="diverges before or at the source"):
        _observation(
            1,
            (0, 10_010, 29_000),
            source="wave-b",
            package="fast-b",
        )


def test_duplicate_seeds_and_mixed_package_evidence_are_rejected() -> None:
    first = _observation(7, (0, 9_000, 29_000))
    with pytest.raises(ValueError, match="unique seeds"):
        calibrate_route_timing_shift_profile_v1((first, first))
    with pytest.raises(ValueError, match="same route and package"):
        calibrate_route_timing_shift_profile_v1((
            first,
            _observation(19, (0, 9_000, 29_000), package="other"),
        ))


def test_independent_nonzero_profiles_do_not_claim_joint_composition() -> None:
    first = calibrate_route_timing_shift_profile_v1((
        _observation(7, (0, 9_000, 29_000)),
    ))
    second = calibrate_route_timing_shift_profile_v1((
        _observation(
            7,
            (0, 10_000, 29_500),
            source="wave-b",
            package="fast-b",
        ),
    ))
    with pytest.raises(ValueError, match="do not prove multi-package composition"):
        persistent_route_time_shift_bounds_v1((first, second))


def test_typed_profile_integrates_only_when_scalar_semantics_are_valid() -> None:
    profile = calibrate_route_timing_shift_profile_v1((
        _observation(7, (0, 9_000, 29_000)),
        _observation(19, (0, 9_200, 29_200)),
    ))
    death_wish = CooldownUseV1(
        "death-wish", "death_wish", 180_000, 0, "SPELL"
    )
    cells = (
        EncounterCooldownCellV1(
            "wave-a",
            TRASH,
            0,
            (CooldownPackageV1(
                "fast-a",
                (death_wish,),
                5,
                encounter_elapsed_ms_delta_min=-1_000,
                encounter_elapsed_ms_delta_max=-800,
            ),),
        ),
        EncounterCooldownCellV1("wave-b", TRASH, 10_000, ()),
        EncounterCooldownCellV1("boss", TRASH, 30_000, ()),
    )

    result = allocate_raid_cooldown_packages_v1(
        cells,
        route_timing_profiles=(profile,),
    )
    assert result.assignments[0].package.package_id == "fast-a"
    assert result.assignments[0].downstream_route_shift_ms_min == -1_000
    assert result.assignments[0].downstream_route_shift_ms_max == -800


def test_allocator_rejects_absorbed_typed_profile_instead_of_persisting_it() -> None:
    absorbed = calibrate_route_timing_shift_profile_v1((
        _observation(7, (0, 9_000, 30_000)),
    ))
    cells = (
        EncounterCooldownCellV1(
            "wave-a",
            TRASH,
            0,
            (CooldownPackageV1(
                "fast-a",
                (),
                5,
                encounter_elapsed_ms_delta_min=-1_000,
                encounter_elapsed_ms_delta_max=-1_000,
            ),),
        ),
        EncounterCooldownCellV1("wave-b", TRASH, 10_000, ()),
        EncounterCooldownCellV1("boss", TRASH, 30_000, ()),
    )
    with pytest.raises(ValueError, match="edge-dependent route timing"):
        allocate_raid_cooldown_packages_v1(
            cells,
            route_timing_profiles=(absorbed,),
        )


def test_allocator_rejects_raw_and_typed_timing_inputs_together() -> None:
    profile = calibrate_route_timing_shift_profile_v1((
        _observation(7, (0, 9_000, 29_000)),
    ))
    cells = (
        EncounterCooldownCellV1("wave-a", TRASH, 0, ()),
        EncounterCooldownCellV1("wave-b", TRASH, 10_000, ()),
        EncounterCooldownCellV1("boss", TRASH, 30_000, ()),
    )
    with pytest.raises(ValueError, match="mutually exclusive"):
        allocate_raid_cooldown_packages_v1(
            cells,
            route_time_shift_bounds={},
            route_timing_profiles=(profile,),
        )
