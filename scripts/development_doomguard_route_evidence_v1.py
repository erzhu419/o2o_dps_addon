"""Print a small observed target-order summary for the frozen d900 trash wave."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from o2o_dps.upper_kara_observed_route_evidence_v1 import extract_selected_wave_gzip_v1


INSTANCE = "d900a97b-b53e-4444-943b-3e0f2be8d477"
ENCOUNTER = "02829cd0-85c3-4b6f-adba-059398e6ae14"
WAVE = f"{ENCOUNTER}:external-v2-wave:1"
FOCAL = "0x0000000000576754"
TARGETS = (
    "0xF13000F240276CB6",
    "0xF13000F244276CB4",
    "0xF13000F245276CB3",
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage5", type=Path, required=True)
    parser.add_argument("--bin-ms", type=int, default=1_000)
    args = parser.parse_args()
    result = extract_selected_wave_gzip_v1(
        args.stage5,
        instance_id=INSTANCE,
        encounter_id=ENCOUNTER,
        wave_id=WAVE,
        target_guids=TARGETS,
        focal_guid=FOCAL,
        bin_width_ms=args.bin_ms,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
