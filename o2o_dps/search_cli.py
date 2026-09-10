"""Run a bounded fixed-seed beam search against the Windows O2O simulator."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Sequence

from .beam_search import SearchError, beam_search
from .sim_bridge import SimBridgeError, SimulatorBridge


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BRIDGE = PROJECT_ROOT / "bin" / "o2obridge.exe"
DEFAULT_FURY_SPELL_IDS = (
    11567,  # lower-rank Heroic Strike retained for the phase-1 fixture
    25286,  # Heroic Strike
    20569,  # Cleave
    23894,  # Bloodthirst
    1680,   # Whirlwind
    20662,  # Execute
    45961,  # Turtle Slam
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", type=Path, help="RaidSimRequest protobuf JSON")
    parser.add_argument("--bridge", type=Path, default=DEFAULT_BRIDGE)
    parser.add_argument("--seed", type=int, default=20260828)
    parser.add_argument("--depth", type=int, default=3)
    parser.add_argument("--beam-width", type=int, default=4)
    parser.add_argument(
        "--spell-id",
        type=int,
        action="append",
        dest="spell_ids",
        help="legal search spell ID; repeat to override the Fury bootstrap set",
    )
    parser.add_argument("--output", type=Path, help="optional result JSON")
    args = parser.parse_args(argv)

    try:
        request = json.loads(args.request.read_text(encoding="utf-8"))
        if not isinstance(request, dict):
            raise ValueError("RaidSimRequest must be a JSON object")
        spell_ids = (
            tuple(args.spell_ids)
            if args.spell_ids is not None
            else DEFAULT_FURY_SPELL_IDS
        )
        with SimulatorBridge(args.bridge) as bridge:
            result = beam_search(
                bridge,
                request,
                seed=args.seed,
                depth=args.depth,
                beam_width=args.beam_width,
                spell_id_allowlist=spell_ids,
            )
        document = {
            "schema_version": 1,
            "kind": "fixed_seed_simulator_teacher_candidate",
            "validation_status": "wowsims_turtle_uncalibrated_not_real_game_improvement",
            "created_at": datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            "request": str(args.request.resolve()),
            "bridge": str(args.bridge.resolve()),
            "seed": args.seed,
            "depth": args.depth,
            "beam_width": args.beam_width,
            "spell_id_allowlist": list(spell_ids),
            "result": result.to_dict(),
        }
        rendered = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8", newline="\n")
        print(rendered, end="")
        return 0
    except (OSError, json.JSONDecodeError, ValueError, SimBridgeError, SearchError) as error:
        print(f"simulator search failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
