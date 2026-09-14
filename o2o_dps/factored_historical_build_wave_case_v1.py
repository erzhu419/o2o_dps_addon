"""Bind one exact historical character build to any declared model wave.

Only the character-owned request fields are transplanted. Encounter targets,
team process, attackability, seed and terminal contract remain those of the
destination development case.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Mapping

from .development_historical_build_wave_case_v1 import (
    DEFAULT_ITEM_DATABASE,
    build_historical_representative_development_wave_case_v1,
)
from .development_wave_case_v1 import DevelopmentWaveCaseV1
from .fury_contra_adapter_v2 import ContraEvidenceKindV2, ContraFieldEvidenceV2
from .fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from .fury_paired_multiseed_runner_v2 import sha256_json
from .historical_representative_character_profile_v1 import DEFAULT_SELECTOR_MANIFEST


SCHEMA = "factored_historical_build_wave_case/v1"


def bind_historical_build_to_wave_case_v1(
    destination: DevelopmentWaveCaseV1, *, rank: int,
    item_database_path: Path = DEFAULT_ITEM_DATABASE,
    selector_manifest_path: Path = DEFAULT_SELECTOR_MANIFEST,
    profile_path_overrides: Mapping[str, str | Path] | None = None,
) -> DevelopmentWaveCaseV1:
    """Return an exact build on a destination model, without copying its pull."""

    seed = destination.dynamic_load.seed
    source = build_historical_representative_development_wave_case_v1(
        seed, rank=rank, item_database_path=item_database_path,
        selector_manifest_path=selector_manifest_path,
        profile_path_overrides=profile_path_overrides,
    )
    request = deepcopy(destination.request)
    source_player = source.request["raid"]["parties"][0]["players"][0]
    request["raid"]["parties"][0]["players"][0] = deepcopy(source_player)
    load = DynamicRolloutLoadV3.bind(request, seed, destination.dynamic_load.config)
    equipment_evidence = ContraFieldEvidenceV2(
        ContraEvidenceKindV2.PINNED_STATIC_INPUT,
        source_sha256=sha256_json(request),
    )
    equipped_names = source.target_contexts[0].equipped_item_names
    contexts = {
        index: replace(
            context,
            equipped_item_names=equipped_names,
            equipment_evidence=equipment_evidence,
        )
        for index, context in destination.target_contexts.items()
    }
    source_spec = source.case_spec
    spec = deepcopy(destination.case_spec)
    spec.update({
        "schema": SCHEMA,
        "source_build_ref": deepcopy(source_spec["source_build_ref"]),
        "build_ref": source_spec["build_ref"],
        "build_role": "EXACT_HISTORICAL_BUILD_TRANSPLANTED_TO_MODEL_WAVE",
        "character": deepcopy(source_spec["character"]),
        "equipment_items": deepcopy(source_spec["equipment_items"]),
        "consumable_inventory": "NOT_OBSERVED; NONE_CONFIGURED_FOR_MODEL",
        "historical_player_policy_used": False,
        "historical_build": deepcopy(source_spec["historical_build"]),
        "build_transplant": {
            "representative_rank": rank,
            "character_fields_source": source_spec["build_ref"],
            "wave_fields_source": destination.case_spec["schema"],
            "destination_source_wave_ref": destination.case_spec["source_wave_ref"],
            "copied_source_pull_state": False,
            "copied_source_team_process": False,
        },
        "request_sha256": load.request_sha256,
        "dynamic_load_contract_sha256": load.contract_sha256,
    })
    if "baseline_fidelity" in source_spec:
        spec["baseline_fidelity"] = deepcopy(source_spec["baseline_fidelity"])
    return DevelopmentWaveCaseV1(spec, request, load, contexts)


__all__ = ("bind_historical_build_to_wave_case_v1",)
