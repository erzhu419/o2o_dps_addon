from __future__ import annotations

import copy
import hashlib
import json
from types import SimpleNamespace
import unittest

from o2o_dps.fury_loadout_armor_nuisance_canary_v1 import (
    FuryLoadoutArmorCanaryError,
    STARTING_ARMORS,
    TARGET_STRATA,
    DURATION_STRATA,
    _canonical_bytes,
    _catalog_canonical_bytes,
    compare_passes,
    request_with_loadout_and_armor,
    select_canary_families,
    select_loadout_captures,
)


_ASSERTIONS = unittest.TestCase()


NONWEAPON = {
    1: (21329, 0),
    2: (18404, 0),
    3: (21330, 3038),
    5: (23226, 1891),
    6: (19137, 92),
    7: (61365, 2543),
    8: (19387, 1887),
    9: (22936, 1885),
    10: (19143, 0),
    11: (19384, 0),
    12: (17063, 0),
    13: (19406, 0),
    14: (60501, 0),
    15: (21394, 849),
}


def _capture(item: int, enchant: int, source: str, line: int, *, talents: int = 16):
    equipment = [
        {"slot": slot, "item_id": values[0], "enchant_id": values[1], "link": str(values[0])}
        for slot, values in sorted(NONWEAPON.items())
    ]
    equipment.append(
        {"slot": 16, "item_id": item, "enchant_id": enchant, "link": str(item)}
    )
    state = {
        "actor": {"class_file": "WARRIOR", "player_guid": "player"},
        "equipment": equipment,
        "talents": [
            {"tab": 2, "index": index + 1, "rank": 1, "name": f"t{index}"}
            for index in range(talents)
        ],
    }
    binding = {
        "source_jsonl": source,
        "source_jsonl_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "source_line": line,
        "sequence": line,
        "captured_at": float(line),
        "event": "STATIC_PROFILE_CAPTURED",
        "task": {"task_run_id": "run"},
    }
    identity = {
        "source_jsonl_sha256": binding["source_jsonl_sha256"],
        "source_line": line,
        "sequence": line,
        "task_run_id": "run",
        "captured_at": float(line),
        "projected_state": state,
    }
    return {
        "schema": "fury_historical_state_capture/v1",
        "evidence_scope": "CALIBRATION_CAPTURE_BOUND",
        "use_class": "HISTORICAL_PRIOR_ONLY",
        "binding": binding,
        "state": state,
        "capture_id": hashlib.sha256(_catalog_canonical_bytes(identity)).hexdigest(),
    }


def _case_capture_pair_is_same_control_group_and_earliest_is_selected():
    later = _capture(21679, 0, "a.jsonl", 20)
    earlier = _capture(21679, 0, "a.jsonl", 10)
    bonereaver = _capture(17076, 1900, "b.jsonl", 3)
    left, right, audit = select_loadout_captures([later, bonereaver, earlier])
    assert left["capture_id"] == earlier["capture_id"]
    assert right["capture_id"] == bonereaver["capture_id"]
    assert audit["selected_group"]["nonweapon_slot_count"] == 14
    assert audit["selected_group"]["nonzero_talent_entry_count"] == 16


def _case_capture_pair_rejects_empty_talent_group():
    with _ASSERTIONS.assertRaisesRegex(FuryLoadoutArmorCanaryError, "no same-player"):
        select_loadout_captures(
            [
                _capture(21679, 0, "a.jsonl", 1, talents=0),
                _capture(17076, 1900, "b.jsonl", 1, talents=0),
            ]
        )


def _family(target: str, duration: str, horizon: int, suffix: str):
    scenario = SimpleNamespace(
        scenario_id=f"{target}-{duration}-{suffix}",
        horizon_ms=horizon,
    )
    return SimpleNamespace(
        scenario=scenario,
        instance_id="heldout",
        family_id=f"family-{suffix}",
        catalog_relative_path="catalog.json.gz",
        target_count=1,
        target_count_stratum=target,
        duration_stratum=duration,
    )


def _case_family_selection_has_all_cells_and_preserves_ultrashort_audit():
    families = []
    for target in TARGET_STRATA:
        for duration in DURATION_STRATA:
            horizon = {
                "short_le_10s": 1000,
                "medium_10_30s": 20_000,
                "long_gt_30s": 40_000,
            }[duration]
            families.append(_family(target, duration, horizon, "a"))
            families.append(_family(target, duration, horizon + 1, "b"))
    families.append(_family("1", "short_le_10s", 99, "ultra"))
    selected, ultras = select_canary_families(
        SimpleNamespace(families=tuple(families))
    )
    assert len(selected) == 12
    assert {(row.target_count_stratum, row.duration_stratum) for row in selected} == {
        (target, duration) for target in TARGET_STRATA for duration in DURATION_STRATA
    }
    assert len(ultras) == 1
    assert ultras[0]["horizon_ms"] == 99
    assert all(row.scenario.horizon_ms >= 100 for row in selected)


def _case_family_selection_fails_closed_when_cell_is_missing():
    families = [
        _family(target, duration, 1000, "only")
        for target in TARGET_STRATA
        for duration in DURATION_STRATA
        if (target, duration) != ("5+", "long_gt_30s")
    ]
    with _ASSERTIONS.assertRaisesRegex(
        FuryLoadoutArmorCanaryError, "required canary cell"
    ):
        select_canary_families(SimpleNamespace(families=tuple(families)))


def _request():
    return {
        "raid": {
            "parties": [
                {
                    "players": [
                        {
                            "equipment": {"items": [{"id": 999}] * 17},
                            "talentsString": "unchanged",
                        }
                    ]
                }
            ]
        },
        "encounter": {
            "targets": [
                {"stats": [0] * 46},
                {"stats": [0] * 46},
            ]
        },
        "simOptions": {"iterations": 99, "randomSeed": "old"},
    }


def _loadout(item: int, enchant: int):
    equipment = [
        {"slot": slot, "item_id": values[0], "enchant_id": values[1]}
        for slot, values in sorted(NONWEAPON.items())
    ]
    equipment.append({"slot": 16, "item_id": item, "enchant_id": enchant})
    return {"equipment": equipment}


def _case_request_mutation_installs_whole_bundle_and_starting_armor():
    source = _request()
    result = request_with_loadout_and_armor(source, _loadout(17076, 1900), 2861)
    assert source["raid"]["parties"][0]["players"][0]["equipment"]["items"][0]["id"] == 999
    items = result["raid"]["parties"][0]["players"][0]["equipment"]["items"]
    assert len(items) == 17
    assert items[14] == {"id": 17076, "enchant": 1900}
    assert items[15:] == [{}, {}]
    assert all(target["stats"][26] == 2861 for target in result["encounter"]["targets"])
    assert result["simOptions"] == {
        "iterations": 1,
        "randomSeed": "384",
        "interactive": True,
    }
    assert result["raid"]["parties"][0]["players"][0]["talentsString"] == "unchanged"


def _pass_rows(mismatch: bool = False):
    rows = []
    for index in range(384):
        rows.append(
            {
                "episode_key_sha256": f"{index:064x}",
                "canonical_result_sha256": (
                    "f" * 64 if mismatch and index == 7 else f"{index + 1:064x}"
                ),
            }
        )
    return rows


def _case_repeat_comparison_requires_all_384_exact_hashes():
    reference = _pass_rows()
    repeat = copy.deepcopy(reference)
    assert compare_passes(reference, repeat)["deterministic_repeat_passed"] is True
    changed = _pass_rows(mismatch=True)
    result = compare_passes(reference, changed)
    assert result["deterministic_repeat_passed"] is False
    assert result["canonical_hash_mismatch_count"] == 1


def _case_repeat_comparison_rejects_wrong_cardinality():
    with _ASSERTIONS.assertRaisesRegex(FuryLoadoutArmorCanaryError, "exactly 384"):
        compare_passes(_pass_rows()[:-1], _pass_rows())


def _case_all_registered_starting_armors_are_distinct():
    assert STARTING_ARMORS == (4211, 3761, 2861, 1961)
    assert len(set(STARTING_ARMORS)) == 4


class FuryLoadoutArmorNuisanceCanaryV1Tests(unittest.TestCase):
    def test_capture_pair_is_same_control_group_and_earliest_is_selected(self):
        _case_capture_pair_is_same_control_group_and_earliest_is_selected()

    def test_capture_pair_rejects_empty_talent_group(self):
        _case_capture_pair_rejects_empty_talent_group()

    def test_family_selection_has_all_cells_and_preserves_ultrashort_audit(self):
        _case_family_selection_has_all_cells_and_preserves_ultrashort_audit()

    def test_family_selection_fails_closed_when_cell_is_missing(self):
        _case_family_selection_fails_closed_when_cell_is_missing()

    def test_request_mutation_installs_whole_bundle_and_starting_armor(self):
        _case_request_mutation_installs_whole_bundle_and_starting_armor()

    def test_repeat_comparison_requires_all_384_exact_hashes(self):
        _case_repeat_comparison_requires_all_384_exact_hashes()

    def test_repeat_comparison_rejects_wrong_cardinality(self):
        _case_repeat_comparison_rejects_wrong_cardinality()

    def test_all_registered_starting_armors_are_distinct(self):
        _case_all_registered_starting_armors_are_distinct()


if __name__ == "__main__":
    unittest.main()
