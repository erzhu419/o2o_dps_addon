"""Read-only held-out white6603 residual attribution at historical opportunities."""

from __future__ import annotations

import argparse
from collections import defaultdict
import gzip
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from o2o_dps.chronicle_external_teammate_response_model_v1 import ABLATION_D
from o2o_dps.responsive_team_hpc_result_loader_v1 import CurrentSourceDeclarationV1
from o2o_dps.responsive_team_runtime_store_v1 import open_responsive_team_runtime_store_v1
from scripts.development_d900_v6_followup_plan_v1 import COMPONENT, STAGE5_SHA
from scripts.development_d900_v7_initial_delay_calibration_audit_v1 import _wave_summary
from scripts.development_d900_v7_white6603_conditional_audit_v1 import _compact_rows


def _empty() -> dict:
    return {"opportunities": 0, "observed_white6603": 0, "expected_white6603": 0.0}


def _add(bucket: dict, row: dict) -> None:
    bucket["opportunities"] += 1
    bucket["observed_white6603"] += int(row["observed_direct_hostile_white6603"])
    bucket["expected_white6603"] += row["p_direct_hostile_white6603"]


def _finish(bucket: dict) -> dict:
    result = dict(bucket)
    result["residual_observed_minus_expected"] = result["observed_white6603"] - result["expected_white6603"]
    result["observed_to_expected"] = (
        result["observed_white6603"] / result["expected_white6603"]
        if result["expected_white6603"] else None
    )
    return result


def evaluate(stage5: Path, model: object) -> dict:
    overall = _empty()
    by_class = defaultdict(_empty)
    by_spec = defaultdict(_empty)
    by_phase = defaultdict(_empty)
    by_elapsed = defaultdict(_empty)
    by_actor = defaultdict(_empty)
    actor_meta = defaultdict(set)
    actor_waves = defaultdict(set)
    wave_ids = []
    with gzip.open(stage5, "rt", encoding="utf-8") as source:
        for line in source:
            record = json.loads(line)
            wave = _wave_summary(record)
            if wave is None:
                continue
            wave_id = wave["wave_id"]
            wave_ids.append(wave_id)
            duration = wave["duration_ms"]
            for row in _compact_rows(record, model):
                _add(overall, row)
                _add(by_class[row["actor_class"]], row)
                _add(by_spec[(row["actor_class"], row["actor_spec_key"])], row)
                _add(by_actor[row["actor_guid"]], row)
                actor_meta[row["actor_guid"]].add((row["actor_class"], row["actor_spec_key"]))
                actor_waves[row["actor_guid"]].add(wave_id)
                phase = min(3, 4 * row["time_ms"] // max(1, duration))
                _add(by_phase[f"Q{phase + 1}"], row)
                elapsed = "0-3s" if row["time_ms"] < 3000 else "3-9s" if row["time_ms"] < 9000 else "9s+"
                _add(by_elapsed[elapsed], row)
    if len(wave_ids) != 37:
        raise ValueError(f"held-out cohort changed: {len(wave_ids)} waves, expected 37")
    actors = [
        {
            "actor_guid": guid,
            "class_spec_keys": [f"{klass}/{spec}" for klass, spec in sorted(actor_meta[guid])],
            "wave_count": len(actor_waves[guid]), **_finish(bucket),
        }
        for guid, bucket in by_actor.items()
    ]
    actors.sort(key=lambda row: row["residual_observed_minus_expected"], reverse=True)
    net = _finish(overall)["residual_observed_minus_expected"]
    positive = sum(max(0.0, row["residual_observed_minus_expected"]) for row in actors)
    return {
        "schema": "development_d900_v7_white6603_residual_strata/v1",
        "scope": "held-out historical-opportunity conditional raw mark, not free-running sim or DPS",
        "wave_ids": wave_ids,
        "overall": _finish(overall),
        "actor_count": len(actors),
        "positive_actor_residual_sum": positive,
        "negative_actor_residual_sum": net - positive,
        "top_actor_positive_residual_share_of_net": {
            str(n): sum(max(0.0, row["residual_observed_minus_expected"]) for row in actors[:n]) / net
            for n in (1, 3, 10)
        },
        "top_actors": actors[:12],
        "by_class": {key: _finish(value) for key, value in sorted(by_class.items())},
        "by_spec": {
            f"{klass}/{spec}": _finish(value)
            for (klass, spec), value in sorted(by_spec.items())
        },
        "by_normalized_quarter": {key: _finish(value) for key, value in sorted(by_phase.items())},
        "by_elapsed_time": {key: _finish(value) for key, value in sorted(by_elapsed.items())},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage5", type=Path, required=True)
    parser.add_argument("--runtime-store", type=Path, required=True)
    parser.add_argument("--result-sha", required=True)
    parser.add_argument("--model-sha", required=True)
    args = parser.parse_args()
    loaded = open_responsive_team_runtime_store_v1(
        args.runtime_store,
        expected_result_content_sha256=args.result_sha,
        expected_model_content_sha256=args.model_sha,
        variant_id=ABLATION_D,
        current_source=CurrentSourceDeclarationV1(
            stage5_content_sha256=STAGE5_SHA,
            component_id=COMPONENT,
            declared_held_out=True,
        ),
    )
    try:
        result = evaluate(args.stage5, loaded.model)
    finally:
        loaded.model.close()
    result["formal_result_sha"] = args.result_sha
    result["formal_model_sha"] = args.model_sha
    result["source_stage5_sha"] = STAGE5_SHA
    result["heldout_component_id"] = COMPONENT
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
