"""Two new source-only dual-target waves for the fixed target-control rule.

Selection uses only the old50 capsule's executable source fields and hostile
activity duration: nearest q20 and q80 among eligible two-target waves after
excluding the prior dual-target case.  The direct-GUID focal exclusions were
audited once against local source CSVs and embedded for small remote runs.
"""

from __future__ import annotations

from functools import lru_cache
import gzip
import json
from typing import Any

from .development_wave_case_v1 import SOURCE_CAPSULE_BUNDLE_SHA256
from .development_wave_coverage_v1 import DEFAULT_MANIFEST, _model_source_blockers
from .development_wave_stratified_v1 import (
    WAVE_STRATA, build_source_stratified_wave_case_v1,
)


SCHEMA = "development_two_wave_target_selection/v1"
SELECTED = {
    "two_short_q20": (
        "bd5002b9-0cb8-442f-beca-a35d07fcce83:wave:1",
        "fc6074bb-b436-4635-8929-42b166c13b32", 7306,
        {"guid": "0x000000000004AD02", "name": "SOURCE_FOCAL_004AD02",
         "direct_damage_by_target": [2337, 5198], "direct_hit_count_by_target": [3, 5]},
    ),
    "two_long_q80": (
        "05f5abba-6b02-4475-bb00-c7f4851419a6:wave:1",
        "012b65f4-6f79-4706-9ca7-6c95976d0c8d", 13491,
        {"guid": "0x00000000004E36D3", "name": "SOURCE_FOCAL_04E36D3",
         "direct_damage_by_target": [1468, 2057], "direct_hit_count_by_target": [6, 14]},
    ),
}


@lru_cache(maxsize=1)
def source_rows_two_v1() -> dict[str, dict[str, Any]]:
    with gzip.open(DEFAULT_MANIFEST, "rt", encoding="utf-8") as stream:
        manifest = json.load(stream)
    if manifest["content_address"]["sha256"] != SOURCE_CAPSULE_BUNDLE_SHA256:
        raise ValueError("old50 source capsule binding changed")
    pool = sorted((row for row in manifest["scenarios"]
                   if len(row["targets"]) == 2
                   and row["source_identity"]["wave_id"] != WAVE_STRATA["multi_two"]
                   and not _model_source_blockers(row)),
                  key=lambda row: (row["horizon"]["milliseconds"],
                                   row["source_identity"]["wave_id"]))
    if len(pool) != 84:
        raise ValueError("old50 eligible dual-target cohort changed")
    chosen = {
        "two_short_q20": pool[round((len(pool) - 1) * 0.20)],
        "two_long_q80": pool[round((len(pool) - 1) * 0.80)],
    }
    if len({row["source_identity"]["instance_id"] for row in chosen.values()}) != 2:
        raise ValueError("source-only quantiles did not select different raids")
    for stratum, row in chosen.items():
        wave_id, instance_id, duration_ms, _ = SELECTED[stratum]
        if (row["source_identity"]["wave_id"],
            row["source_identity"]["instance_id"],
            row["horizon"]["milliseconds"]) != (wave_id, instance_id, duration_ms):
            raise ValueError("frozen source-only two-wave selection changed")
    return chosen


def build_two_wave_target_case_v1(seed: int, stratum: str):
    if stratum not in SELECTED:
        raise ValueError(f"unknown two-target source stratum: {stratum}")
    source = source_rows_two_v1()[stratum]
    _, _, _, focal = SELECTED[stratum]
    case, scenario = build_source_stratified_wave_case_v1(
        seed, stratum, source, focal, schema=SCHEMA,
    )
    case.case_spec["registry"] = {
        "schema": SCHEMA,
        "selection": "ELIGIBLE_DUAL_TARGET_DURATION_Q20_Q80_SOURCE_ONLY",
        "candidate_wave_count": 84,
        "selected_wave_count": 2,
        "historical_exact": False,
    }
    return case, scenario


__all__ = ["SCHEMA", "SELECTED", "source_rows_two_v1", "build_two_wave_target_case_v1"]
