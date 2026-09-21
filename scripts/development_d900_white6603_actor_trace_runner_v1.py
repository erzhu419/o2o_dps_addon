"""One-seed development replay with early 6603 actor trace; no model changes."""

from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json

from o2o_dps.responsive_white6603_actor_trace_v1 import White6603ActorTraceDrivenBridgeV1
from scripts import development_responsive_upper_kara_trash_full_wave_v1 as full_wave


def main() -> int:
    # This standalone process does not alter the shared full-wave runner used by
    # ongoing paired evaluations. Its CLI and frozen-case checks remain intact.
    full_wave.IncantagosDevelopmentDrivenBridgeV1 = White6603ActorTraceDrivenBridgeV1
    output = StringIO()
    with redirect_stdout(output):
        result_code = full_wave.main()
    if result_code:
        return result_code
    result = json.loads(output.getvalue())
    rows = result["paired_replays"]
    if len(rows) != 1:
        raise ValueError("actor trace requires a single source and seed")
    row = rows[0]
    diagnostic = row["responsive_drive"]["target_event_diagnostic"]
    print(json.dumps({
        "schema": "development_d900_white6603_actor_trace_replay/v1",
        "wave_id": result["wave_id"],
        "source_policy_id": row["source_policy_id"],
        "seed": row["seed"],
        "teammate_seed": row["teammate_seed"],
        "status": row["status"],
        "invalid_reason": row["invalid_reason"],
        "early_direct_white6603_actor_trace": diagnostic["early_direct_white6603_actor_trace"],
        "focus_actor_wake_marks": diagnostic["focus_actor_wake_marks"],
        "focus_actor_deadline_audit": diagnostic["focus_actor_deadline_audit"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
