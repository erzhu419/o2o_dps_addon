"""Print compact historical build versus controlled v4 request diagnostic."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from o2o_dps.historical_v4_build_match_diagnostic_v1 import diagnose_historical_v4_build_match_v1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--encounter-id", required=True)
    parser.add_argument("--focal-guid", required=True)
    parser.add_argument("--wave-reconstruction", type=Path)
    parser.add_argument("--controlled-stop-ms", type=int)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--request", type=Path, default=ROOT / "configs/wowsims/fury_warrior_clean_dual.json")
    parser.add_argument(
        "--join-manifest", type=Path,
        default=ROOT / "offline_data/derived/historical_fury_decision_build_join/v1/manifest.json",
    )
    parser.add_argument(
        "--catalog", type=Path,
        default=ROOT / "offline_data/derived/historical_build_catalog/v1/catalog.jsonl.gz",
    )
    args = parser.parse_args()
    result = diagnose_historical_v4_build_match_v1(
        instance_id=args.instance_id,
        encounter_id=args.encounter_id,
        focal_guid=args.focal_guid,
        controlled_request=json.loads(args.request.read_text(encoding="utf-8")),
        join_manifest_path=args.join_manifest,
        catalog_path=args.catalog,
        wave_reconstruction_path=args.wave_reconstruction,
        controlled_stop_ms=args.controlled_stop_ms,
    )
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
