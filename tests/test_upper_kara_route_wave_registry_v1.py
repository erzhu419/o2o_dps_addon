from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from o2o_dps.upper_kara_route_wave_registry_v1 import (
    TARGET_CONTRACT_AUTHORITATIVE,
    TARGET_CONTRACT_UNRESOLVED,
    UpperKaraRouteRegistryError,
    apply_authoritative_target_overlay_v1,
    build_route_wave_registry_from_admission_v1,
    build_route_wave_registry_v1,
    metadata_document_to_route_observation_v1,
    route_wave_registry_from_dict_v1,
)


def _period(start: str, end: str, *, end_state: str = "slain") -> dict:
    return {
        "start": start,
        "end": end,
        "last_active": end,
        "end_state": end_state,
    }


def _ts(second: int) -> str:
    value = datetime(2026, 9, 8, 13, 0, tzinfo=timezone.utc) + timedelta(
        seconds=second
    )
    return value.isoformat().replace("+00:00", "Z")


def _encounter(
    instance_id: str,
    encounter_id: str,
    name: str,
    start_second: int,
    *,
    boss: bool,
    kill_type: str,
    hostiles: list[dict],
    duration_seconds: int = 10,
) -> dict:
    return {
        "id": encounter_id,
        "instance_id": instance_id,
        "boss": boss,
        "name": name,
        "kill_type": kill_type,
        "start_time": _ts(start_second),
        "end_time": _ts(start_second + duration_seconds),
        "hostiles": hostiles,
    }


def _hostile(
    guid: str,
    start_second: int,
    *,
    boss: bool = False,
    death_after_seconds: int = 8,
    end_state: str = "slain",
) -> dict:
    return {
        "id": guid,
        "boss": boss,
        "periods": [
            _period(
                _ts(start_second),
                _ts(start_second + death_after_seconds),
                end_state=end_state,
            )
        ],
    }


def _document(instance_id: str, *, include_extra_trash: bool = False) -> dict:
    units = {
        f"{instance_id}-trash-a": {"entry": 1001, "name": "Trash A"},
        f"{instance_id}-trash-x": {"entry": 1099, "name": "Optional Trash"},
        f"{instance_id}-boss-a-1": {"entry": 2001, "name": "Boss A"},
        f"{instance_id}-boss-a-2": {"entry": 2001, "name": "Boss A"},
        f"{instance_id}-trash-b": {"entry": 1002, "name": "Trash B"},
        f"{instance_id}-boss-b": {"entry": 2002, "name": "Boss B"},
        f"{instance_id}-add-1": {"entry": 3001, "name": "Priority Add"},
        f"{instance_id}-add-2": {"entry": 3001, "name": "Priority Add"},
    }
    encounters = [
        _encounter(
            instance_id,
            f"{instance_id}-e0",
            "Trash A",
            0,
            boss=False,
            kill_type="clean",
            hostiles=[_hostile(f"{instance_id}-trash-a", 0)],
        )
    ]
    if include_extra_trash:
        encounters.append(
            _encounter(
                instance_id,
                f"{instance_id}-ex",
                "Optional Trash",
                11,
                boss=False,
                kill_type="clean",
                hostiles=[_hostile(f"{instance_id}-trash-x", 11)],
            )
        )
    boss_start = 22 if include_extra_trash else 11
    encounters.extend(
        [
            _encounter(
                instance_id,
                f"{instance_id}-e1",
                "Boss A",
                boss_start,
                boss=True,
                kill_type="wipe",
                hostiles=[
                    _hostile(
                        f"{instance_id}-boss-a-1",
                        boss_start,
                        boss=True,
                        end_state="active",
                    )
                ],
            ),
            _encounter(
                instance_id,
                f"{instance_id}-e2",
                "Boss A",
                boss_start + 11,
                boss=True,
                kill_type="clean",
                hostiles=[
                    _hostile(
                        f"{instance_id}-boss-a-2", boss_start + 11, boss=True
                    )
                ],
            ),
            _encounter(
                instance_id,
                f"{instance_id}-e3",
                "Trash B",
                boss_start + 22,
                boss=False,
                kill_type="clean",
                hostiles=[_hostile(f"{instance_id}-trash-b", boss_start + 22)],
            ),
            _encounter(
                instance_id,
                f"{instance_id}-e4",
                "Boss B",
                boss_start + 33,
                boss=True,
                kill_type="partial",
                duration_seconds=10,
                hostiles=[
                    _hostile(
                        f"{instance_id}-boss-b",
                        boss_start + 33,
                        boss=True,
                        death_after_seconds=10,
                        end_state="active",
                    ),
                    _hostile(
                        f"{instance_id}-add-1",
                        boss_start + 35,
                        death_after_seconds=3,
                    ),
                    _hostile(
                        f"{instance_id}-add-2",
                        boss_start + 35,
                        death_after_seconds=5,
                    ),
                ],
            ),
        ]
    )
    return {
        "id": instance_id,
        "name": "Upper Tower of Karazhan",
        "encounters": encounters,
        "units": units,
    }


def test_metadata_observation_preserves_retry_and_does_not_infer_permission() -> None:
    observation = metadata_document_to_route_observation_v1(_document("raid-a"))
    assert observation["distinct_boss_sequence"] == ["entry-2001", "entry-2002"]
    assert observation["encounters"][1]["attempt_ordinal"] == 1
    assert observation["encounters"][2]["attempt_ordinal"] == 2
    assert observation["encounters"][0]["route_start_offset_ms"] == 0
    assert observation["encounters"][2]["route_start_offset_ms"] > observation["encounters"][1]["route_start_offset_ms"]
    assert observation["encounters"][4]["pull_outcome"] == "PARTIAL"
    assert observation["contains_partial_encounter"] is True
    boss_b = observation["encounters"][4]
    assert boss_b["target_entry_multiset"] == {"2002": 1, "3001": 2}
    assert any(len(phase["active_occurrence_ids"]) == 3 for phase in boss_b["phase_candidates"])
    assert all(
        phase["target_contract"]["status"] == TARGET_CONTRACT_UNRESOLVED
        and phase["target_contract"]["allowed_primary_occurrence_ids"] == []
        for phase in boss_b["phase_candidates"]
    )


def test_metadata_observation_accepts_chronicle_two_digit_fraction_with_z_and_offset() -> None:
    document = _document("raid-a")
    encounter = document["encounters"][0]
    encounter["start_time"] = "2026-09-08T21:00:00.57+08:00"
    encounter["end_time"] = "2026-09-08T13:00:10.57Z"
    period = encounter["hostiles"][0]["periods"][0]
    period["start"] = "2026-09-08T13:00:00.57Z"
    period["end"] = "2026-09-08T21:00:08.57+08:00"
    period["last_active"] = "2026-09-08T13:00:08.57Z"

    observation = metadata_document_to_route_observation_v1(document)

    first = observation["encounters"][0]
    assert first["duration_ms"] == 10_000
    assert first["route_start_offset_ms"] == 0
    assert first["targets"][0]["first_activity_offset_ms"] == 0
    assert first["targets"][0]["last_activity_offset_ms"] == 8_000


def test_registry_uses_boss_anchored_ordered_alignment() -> None:
    registry = build_route_wave_registry_v1(
        [_document("raid-a"), _document("raid-b", include_extra_trash=True)]
    )
    assert registry["summary"]["instance_count"] == 2
    assert registry["summary"]["route_variant_count"] == 1
    variant = registry["route_variants"][0]
    assert variant["trash_alignment"] == "BOSS_ANCHORED_SEGMENT_LOCAL_EXACT_SIGNATURE_LCS"
    by_instance = {row["instance_id"]: row for row in registry["instances"]}
    a = by_instance["raid-a"]["encounters"]
    b = by_instance["raid-b"]["encounters"]
    assert a[0]["pull_slot_id"] == b[0]["pull_slot_id"]
    assert a[1]["pull_slot_id"] == a[2]["pull_slot_id"]
    assert b[2]["pull_slot_id"] == b[3]["pull_slot_id"]
    assert "insert-" in b[1]["pull_slot_id"] or "pull-" in b[1]["pull_slot_id"]
    assert a[3]["pull_slot_id"] == b[4]["pull_slot_id"]


def test_authoritative_overlay_keeps_direct_and_collateral_separate() -> None:
    registry = build_route_wave_registry_v1([_document("raid-a")])
    encounter = registry["instances"][0]["encounters"][4]
    phase = next(
        row for row in encounter["phase_candidates"] if len(row["active_occurrence_ids"]) == 3
    )
    boss = next(
        row["occurrence_id"] for row in encounter["targets"] if row["creature_entry_id"] == 2002
    )
    adds = [
        row["occurrence_id"] for row in encounter["targets"] if row["creature_entry_id"] == 3001
    ]
    resolved = apply_authoritative_target_overlay_v1(
        registry,
        {
            "pull_ref": encounter["pull_ref"],
            "phase_ref": phase["phase_ref"],
            "source_kind": "USER_AUTHORITATIVE",
            "source_ref": "raid-leader:adds-first",
            "allowed_primary_occurrence_ids": adds,
            "forbidden_primary_occurrence_ids": [boss],
            "priority_partial_order": [[adds[0], adds[1]]],
            "collateral_occurrence_ids": [boss, *adds],
        },
    )
    changed = next(
        row
        for row in resolved["instances"][0]["encounters"][4]["phase_candidates"]
        if row["phase_ref"] == phase["phase_ref"]
    )["target_contract"]
    assert changed["status"] == TARGET_CONTRACT_AUTHORITATIVE
    assert changed["allowed_primary_occurrence_ids"] == adds
    assert changed["forbidden_primary_occurrence_ids"] == [boss]
    assert boss in changed["collateral_occurrence_ids"]
    assert registry["summary"]["authoritative_target_contract_count"] == 0
    assert resolved["summary"]["authoritative_target_contract_count"] == 1


def test_non_authoritative_overlay_is_rejected() -> None:
    registry = build_route_wave_registry_v1([_document("raid-a")])
    encounter = registry["instances"][0]["encounters"][0]
    target = encounter["targets"][0]["occurrence_id"]
    with pytest.raises(UpperKaraRouteRegistryError, match="not authoritative"):
        apply_authoritative_target_overlay_v1(
            registry,
            {
                "pull_ref": encounter["pull_ref"],
                "phase_ref": encounter["phase_candidates"][0]["phase_ref"],
                "source_kind": "INFERRED_FROM_DAMAGE",
                "source_ref": "log-majority",
                "allowed_primary_occurrence_ids": [target],
                "forbidden_primary_occurrence_ids": [],
                "priority_partial_order": [],
                "collateral_occurrence_ids": [target],
            },
        )


def test_registry_roundtrip_is_strict() -> None:
    registry = build_route_wave_registry_v1([_document("raid-a")])
    wire = json.loads(json.dumps(registry))
    assert route_wave_registry_from_dict_v1(wire) == registry
    broken = deepcopy(wire)
    broken["instances"][0]["encounters"][0]["phase_candidates"][0][
        "target_contract"
    ]["observed_activity_grants_target_permission"] = True
    with pytest.raises(UpperKaraRouteRegistryError, match="granted permission"):
        route_wave_registry_from_dict_v1(broken)


def test_metadata_encounters_are_put_in_strict_chronological_ordinal_order() -> None:
    document = _document("raid-a")
    document["encounters"][1], document["encounters"][2] = (
        document["encounters"][2],
        document["encounters"][1],
    )
    observation = metadata_document_to_route_observation_v1(document)
    encounters = observation["encounters"]
    assert [row["encounter_ordinal"] for row in encounters] == list(
        range(len(encounters))
    )
    assert [row["metadata_encounter_array_ordinal"] for row in encounters][1:3] == [2, 1]


def test_admission_loader_reads_only_named_metadata_objects(tmp_path: Path) -> None:
    offline = tmp_path / "offline_data"
    object_root = offline / "chronicle_raw" / "external_api" / "v1"
    (object_root / "manifests").mkdir(parents=True)
    object_path = object_root / "objects" / "sha256" / "aa" / "raid.json"
    object_path.parent.mkdir(parents=True)
    document = _document("raid-a")
    object_path.write_text(json.dumps(document), encoding="utf-8")
    admission = {
        "schema": "chronicle_external_reconstruction_admission/v1",
        "inputs": {
            "raw_api_manifest": {
                "path": "chronicle_raw/external_api/v1/manifests/raw.json"
            }
        },
        "instances": [
            {
                "instance_id": "raid-a",
                "source_evidence": {
                    "metadata_object": {
                        "relative_path": "objects/sha256/aa/raid.json",
                        "size_bytes": object_path.stat().st_size,
                    }
                },
            }
        ],
    }
    admission_path = tmp_path / "admission.json"
    admission_path.write_text(json.dumps(admission), encoding="utf-8")
    registry = build_route_wave_registry_from_admission_v1(
        admission_path,
        offline_data_root=offline,
    )
    assert registry["source"]["metadata_object_count"] == 1
    assert registry["source"]["event_streams_read"] == 0
    assert registry["summary"]["raw_or_normalized_rows_read"] == 0
