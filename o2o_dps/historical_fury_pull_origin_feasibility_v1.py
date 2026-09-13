"""Classify whether the nine old Chronicle windows can start an exact pull replay.

Chronicle state events contain changes, not a complete absolute player or target
state at pull origin.  A replay from an unknown origin is still an unknown state;
the existing strict-prefix checkpoint manifest is the source of the per-window
missing-field evidence.  This module does not reinterpret a later kill budget
or an absent event as an origin value.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .historical_fury_source_bound_prefix_checkpoint_v1 import (
    DEFAULT_OUTPUT_DIRECTORY as PREFIX_DIRECTORY,
    REQUIRED_FIELDS,
    validate_manifest as validate_prefix_manifest,
)


SCHEMA = "historical_fury_pull_origin_feasibility/v1"
DEFAULT_PREFIX_MANIFEST = PREFIX_DIRECTORY / "manifest.json"
DEFAULT_OUTPUT = (
    Path(__file__).resolve().parents[1]
    / "offline_data"
    / "derived"
    / "historical_fury_pull_origin_feasibility"
    / "v1"
    / "manifest.json"
)

# A pull-origin replay is not an alternative to measuring these absolute values.
# Event deltas only become useful after a complete origin checkpoint exists.
ORIGIN_BASELINE_FIELDS = (
    "target.max_health",
    "target.current_health",
    "player.rage_current",
    "player.stance",
    "timers.gcd_remaining_ms",
    "timers.cooldowns_remaining_ms",
    "timers.main_hand_swing_remaining_ms",
    "timers.off_hand_swing_remaining_ms",
    "queue.next_swing",
    "player.self_auras_and_procs",
    "target.candidate_owned_existing_debuffs",
)


def compile_feasibility(prefix_manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Produce a small decision manifest from the verified existing evidence."""
    source = validate_prefix_manifest(prefix_manifest)
    if tuple(source["checkpoint_contract"]["required_fields"]) != REQUIRED_FIELDS:
        raise ValueError("unexpected strict-prefix checkpoint contract")
    if tuple(ORIGIN_BASELINE_FIELDS) != REQUIRED_FIELDS:
        raise ValueError("pull-origin baseline and checkpoint contract diverged")

    rows: list[dict[str, Any]] = []
    for row in source["rows"]:
        fields = row["fields"]
        unknown = [
            name
            for name in ORIGIN_BASELINE_FIELDS
            if fields[name]["observation_category"] != "EXACT"
        ]
        # These old inputs are Chronicle streams alone, with no live pull-origin
        # checkpoint.  Even an exact *window* stance does not establish that the
        # origin has a complete player/target state suitable for forward replay.
        rows.append(
            {
                "source_identity": row["source_identity"],
                "segment_ref": row["segment_ref"],
                "window_cutoff_exclusive_order_key": row["window_start"][
                    "cutoff_exclusive_order_key"
                ],
                "window_checkpoint_exact": row["exact_checkpoint_ready"],
                "window_nonexact_fields": unknown,
                "pull_origin_checkpoint_observed": False,
                "strict_pull_replay_exact": False,
                "cohort": "HISTORICAL_PARTIAL_OFFLINE_PRIOR",
                "reason": "NO_ABSOLUTE_PULL_ORIGIN_CHECKPOINT_IN_CHRONICLE_INPUT",
            }
        )

    return {
        "schema": SCHEMA,
        "source_prefix_manifest_sha256": source["content_address"]["sha256"],
        "source_scope": "CHRONICLE_STATE_STREAMS_ONLY_NO_LIVE_CHECKPOINT",
        "rows": rows,
        "summary": {
            "window_count": len(rows),
            "exact_pull_replay_count": 0,
            "historical_partial_count": len(rows),
        },
        "next_exact_path": "NEW_LIVE_SHADOW_CHECKPOINT_AND_SAME_PULL_STRICT_REPLAY",
        "authorization": {
            "exact_source_comparison": False,
            "policy_value_training": False,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix-manifest", type=Path, default=DEFAULT_PREFIX_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    with args.prefix_manifest.open("r", encoding="utf-8") as handle:
        result = compile_feasibility(json.load(handle))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result["summary"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
