"""Materialize an observed Fury build with explicitly controlled run settings."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from o2o_dps.exact_historical_fury_wowsims_request_v1 import (
    compose_exact_historical_fury_wowsims_request_v1,
    load_exact_historical_fury_segment_v1,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--encounter-id", required=True)
    parser.add_argument("--focal-guid", required=True)
    parser.add_argument("--segment-id", required=True)
    parser.add_argument("--catalog", type=Path, default=ROOT / "offline_data/derived/historical_build_catalog/v1/catalog.jsonl.gz")
    parser.add_argument("--controlled-request", type=Path, default=ROOT / "configs/wowsims/fury_warrior_clean_dual.json")
    parser.add_argument("--item-db", type=Path, default=ROOT.parent / "wowsims-turtle/assets/database/db.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    line_number, segment = load_exact_historical_fury_segment_v1(
        args.catalog,
        instance_id=args.instance_id,
        encounter_id=args.encounter_id,
        focal_guid=args.focal_guid,
        segment_id=args.segment_id,
    )
    controlled = json.loads(args.controlled_request.read_text(encoding="utf-8"))
    result = compose_exact_historical_fury_wowsims_request_v1(
        segment,
        catalog_path=args.catalog,
        catalog_line_number=line_number,
        controlled_request=controlled,
    )
    item_names = {
        row["id"]: row["name"]
        for row in json.loads(args.item_db.read_text(encoding="utf-8"))["items"]
    }
    item_ids = [
        row["id"]
        for row in result["request"]["raid"]["parties"][0]["players"][0]
        ["equipment"]["items"]
        if row.get("id")
    ]
    if any(item_id not in item_names for item_id in item_ids):
        raise ValueError("historical equipment lacks Wowsims item names")
    result["equipped_item_names"] = [item_names[item_id] for item_id in item_ids]
    result["equipped_item_name_source"] = "wowsims-turtle/assets/database/db.json"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
