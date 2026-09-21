from __future__ import annotations

import json
from pathlib import Path

import pytest

from o2o_dps.exact_historical_fury_wowsims_request_v1 import (
    compose_exact_historical_fury_wowsims_request_v1,
    load_exact_historical_fury_segment_v1,
)


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "offline_data/derived/historical_build_catalog/v1/catalog.jsonl.gz"
CONTROLLED = ROOT / "configs/wowsims/fury_warrior_clean_dual.json"


@pytest.mark.skipif(not CATALOG.exists(), reason="local historical catalog is unavailable")
def test_d900_segment_0042_preserves_exact_tauren_dual_wield_build():
    line_number, segment = load_exact_historical_fury_segment_v1(
        CATALOG,
        instance_id="d900a97b-b53e-4444-943b-3e0f2be8d477",
        encounter_id="02829cd0-85c3-4b6f-adba-059398e6ae14",
        focal_guid="0x0000000000576754",
        segment_id="segment-0042",
    )
    controlled = json.loads(CONTROLLED.read_text(encoding="utf-8"))
    result = compose_exact_historical_fury_wowsims_request_v1(
        segment,
        catalog_path=CATALOG,
        catalog_line_number=line_number,
        controlled_request=controlled,
    )
    assert line_number == 80251
    assert result["status"] == "DEVELOPMENT_CHARACTER_BUILD_ONLY"
    assert result["comparison_authorized"] is False
    assert result["historical_runtime_executable"] is True
    player = result["request"]["raid"]["parties"][0]["players"][0]
    assert player["race"] == "RaceTauren"
    assert player["talentsString"] == "30205020302-05050005525010051"
    assert player["equipment"]["items"][14] == {"id": 23054, "enchant": 1900}
    assert player["equipment"]["items"][15] == {"id": 23577, "enchant": 1900}
    assert player["warrior"]["options"]["startingRage"] == 100
    assert controlled["raid"]["parties"][0]["players"][0]["race"] == "RaceGnome"
