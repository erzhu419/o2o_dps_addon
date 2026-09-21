"""Check that an observed-build development request loads in the real bridge."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from o2o_dps.sim_bridge import SimulatorBridge


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge", type=Path, required=True)
    parser.add_argument("--simulator-root", type=Path, required=True)
    parser.add_argument("--materialized-request", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=2026092001)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    materialized = json.loads(args.materialized_request.read_text(encoding="utf-8"))
    if materialized["status"] != "DEVELOPMENT_CHARACTER_BUILD_ONLY":
        raise ValueError("historical build request was not admitted")
    request = materialized["request"]
    player = request["raid"]["parties"][0]["players"][0]
    with SimulatorBridge(args.bridge, cwd=args.simulator_root) as bridge:
        state = bridge.load(request, args.seed)
        actions = bridge.actions()
    result = {
        "schema": "development_exact_build_bridge_load/v1",
        "status": "LOADED",
        "comparison_authorized": False,
        "source_segment_id": materialized["source_identity"]["build_segment_id"],
        "race": player["race"],
        "talents_string": player["talentsString"],
        "main_hand_item_id": player["equipment"]["items"][14].get("id"),
        "off_hand_item_id": player["equipment"]["items"][15].get("id"),
        "initial_time_ms": state["time_ms"],
        "initial_rage": state["power"]["current"],
        "bloodthirst_known": any(row.action.spell_id == 23894 for row in actions),
        "whirlwind_known": any(row.action.spell_id == 1680 for row in actions),
    }
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
