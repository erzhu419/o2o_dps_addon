"""Run the frozen one-seed d900 three-source replay with the formal v6 D store."""

from __future__ import annotations

import json
from pathlib import Path
import sys

from development_d900_v6_followup_plan_v1 import BASE, build_plan_v1


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = f"{BASE}/runtime-src-20260921-causal-target-v6"
DISPATCH = (
    f"{BASE}/runs/teammate-response-current/single_scan_causal_target_choice_v6/"
    "7484e06074ef5a7a02a2bf6e06dcbca252777081040f75bfb2142616321c211a/"
    "attempt1/dispatch/dispatch.json"
)
RESULT_SHA = "8fc29addf548011ab72471758ae4fe96444633fca8df6146e1c31d7f0764f125"
MODEL_SHA = "a82aa8858dc8b2e7ad453ed5494cc39476d122a4c4eeb23149a8a93fb034fe09"
OUTPUT = ROOT / "results/responsive-team-v4/v55-d900-v6-formal-d-full-wave-seed1.json"


def main() -> None:
    sys.path.insert(0, str(Path.home() / "mine_code/scheduleurm/skill"))
    import scheduler

    plan = build_plan_v1(
        code_root=RUNTIME,
        simulator_root=RUNTIME,
        frozen_dispatch=DISPATCH,
        runtime_store=f"{RUNTIME}/results/formal-d-causal-target-v6.sqlite3",
        result_sha=RESULT_SHA,
        model_sha=MODEL_SHA,
        bridge=(f"{RUNTIME}/bin/"
                "o2obridge.seedfix-v26.observed-damage-v2.withdb.goamd64v1.linux-amd64"),
    )
    rc, stdout, stderr = scheduler.run_on(
        "node004",
        f"PYTHONDONTWRITEBYTECODE=1 PYTHONPATH={RUNTIME} "
        + plan["full_wave"]["shell_command"],
        timeout=1200,
        check=False,
    )
    if rc:
        raise RuntimeError(f"d900 full-wave failed (rc={rc}): {stderr[-2000:]}")
    result = json.loads(stdout)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(OUTPUT),
        "status": result["status"],
        "comparison_authorized": result["comparison_authorized"],
        "model_sha": MODEL_SHA,
        "source_result_sha": RESULT_SHA,
        "paired_replays": [{
            "source_policy_id": row["source_policy_id"],
            "status": row["status"],
            "effective_damage": row["effective_damage"],
            "valid_development_completion": row["valid_development_completion"],
            "invalid_reason": row["invalid_reason"],
        } for row in result["paired_replays"]],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
