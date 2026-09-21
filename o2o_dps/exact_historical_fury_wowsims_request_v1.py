"""Compose one exact historical Fury character into a development Wowsims request."""

from __future__ import annotations

from copy import deepcopy
import gzip
import json
from pathlib import Path
from typing import Any, Mapping

from .build_request_composer_v1 import (
    EncounterModel,
    ExecutionModel,
    Objective,
    RaidContext,
    compose_build_request_v1,
)
from .historical_build_catalog_v1 import historical_segment_to_character_profile


SCHEMA = "exact_historical_fury_wowsims_request/v1"


def load_exact_historical_fury_segment_v1(
    catalog_path: Path,
    *,
    instance_id: str,
    encounter_id: str,
    focal_guid: str,
    segment_id: str,
) -> tuple[int, dict[str, Any]]:
    """Select the observed segment that starts in this exact encounter."""

    with gzip.open(catalog_path, "rt", encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            if instance_id not in line or segment_id not in line or focal_guid.lower() not in line.lower():
                continue
            segment = json.loads(line)
            identity = segment["identity"]
            if (
                identity["instance_id"] == instance_id
                and identity["player_guid"].lower() == focal_guid.lower()
                and identity["build_segment_id"] == segment_id
                and segment["observation"]["valid_from"]["encounter_id"] == encounter_id
            ):
                return line_number, segment
    raise ValueError("exact historical Fury segment was not found in the catalog")


def compose_exact_historical_fury_wowsims_request_v1(
    segment: Mapping[str, Any],
    *,
    catalog_path: Path,
    catalog_line_number: int,
    controlled_request: Mapping[str, Any],
) -> dict[str, Any]:
    """Use observed character fields and independently controlled run fields."""

    if segment["player"]["hero_class"] != "WARRIOR" or not any(
        row.get("talent_id") == "warrior.bloodthirst" and row.get("rank") == 1
        for row in segment["talents"]["semantic_ranks"]
    ):
        raise ValueError("selected historical segment is not a learned-Bloodthirst Fury build")
    character = historical_segment_to_character_profile(
        segment,
        catalog_path=catalog_path,
        catalog_line_number=catalog_line_number,
        consumes={},
        database={},
    )
    base_raid = controlled_request["raid"]
    base_parties = base_raid["parties"]
    base_party = base_parties[0]
    base_player = base_party["players"][0]
    options = deepcopy(base_player.get("warrior", {}).get("options", {}))
    options.pop("ravagerRank", None)  # this is derived from historical talents
    composition = compose_build_request_v1(
        character,
        RaidContext(
            individual_buffs=deepcopy(base_player.get("buffs", {})),
            party_buffs=deepcopy(base_party.get("buffs", {})),
            raid_buffs=deepcopy(base_raid.get("buffs", {})),
            debuffs=deepcopy(base_raid.get("debuffs", {})),
            additional_party_players=deepcopy(base_party["players"][1:]),
            additional_parties=deepcopy(base_parties[1:]),
            raid_options={
                key: deepcopy(value) for key, value in base_raid.items()
                if key not in {"parties", "buffs", "debuffs"}
            },
            provenance={"source": SCHEMA, "role": "CONTROLLED_RAID_CONTEXT"},
        ),
        EncounterModel(
            request_fields=deepcopy(controlled_request["encounter"]),
            provenance={"source": SCHEMA, "role": "CONTROLLED_ENCOUNTER"},
        ),
        ExecutionModel(
            rotation=deepcopy(base_player.get("rotation", {})),
            cooldowns=deepcopy(base_player.get("cooldowns", {})),
            warrior_options=options,
            reaction_time_ms=base_player.get("reactionTimeMs", 0),
            channel_clip_delay_ms=base_player.get("channelClipDelayMs", 0),
            in_front_of_target=base_player.get("inFrontOfTarget", False),
            distance_from_target=base_player.get("distanceFromTarget", 5),
            provenance={"source": SCHEMA, "role": "CONTROLLED_EXECUTION_RESET"},
        ),
        Objective(
            kind="DEVELOPMENT_MODEL_DEFINED_WAVE_EFFECTIVE_DAMAGE",
            sim_options=deepcopy(controlled_request["simOptions"]),
            provenance={"source": SCHEMA, "role": "CONTROLLED_OBJECTIVE"},
        ),
    )
    return {
        "schema": SCHEMA,
        "status": "DEVELOPMENT_CHARACTER_BUILD_ONLY" if composition.admitted else "NOT_ADMITTED",
        "comparison_authorized": False,
        "source_identity": deepcopy(segment["identity"]),
        "source_player": deepcopy(segment["player"]),
        "source_valid_from": deepcopy(segment["observation"]["valid_from"]),
        "catalog_line_number": catalog_line_number,
        "historical_runtime_executable": segment["coverage"]["runtime_executable"],
        "request": composition.request,
        "composer_audit": composition.audit,
    }
