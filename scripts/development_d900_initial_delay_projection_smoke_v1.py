"""One-seed, no-emission d900 first-wake context smoke on a formal D store."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys

ROOT = (
    Path(sys.argv[sys.argv.index("--code-root") + 1])
    if "--code-root" in sys.argv
    else Path(__file__).resolve().parents[1]
)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from o2o_dps.chronicle_external_teammate_response_model_v1 import ABLATION_D, _context_keys
from o2o_dps.responsive_incantagos_development_session_v1 import bind_responsive_incantagos_development_session_v1
from o2o_dps.responsive_team_frozen_validation_source_v1 import verify_frozen_validation_source_v1
from o2o_dps.responsive_team_hpc_result_loader_v1 import CurrentSourceDeclarationV1
from o2o_dps.responsive_team_runtime_store_v1 import open_responsive_team_runtime_store_v1
from o2o_dps.sim_bridge_dynamic_v4 import SimulatorBridgeDynamicV4
from scripts.development_responsive_upper_kara_trash_smoke_v1 import INSTANCE_ID, build_case


def run(args: argparse.Namespace) -> dict:
    parts = args.stage5.parts
    locator = Path(*parts[parts.index("offline_data"):]).as_posix()
    verify_frozen_validation_source_v1(
        json.loads(args.frozen_dispatch.read_text(encoding="utf-8")),
        instance_id=INSTANCE_ID, component_id=args.component_id,
        partition_locator=locator,
        stage5_content_sha256=args.stage5_content_sha256,
        partition_compressed_file_sha256=args.partition_compressed_file_sha256,
    )
    fixed, case = build_case(args)
    del fixed
    loaded = open_responsive_team_runtime_store_v1(
        args.runtime_store,
        expected_result_content_sha256=args.result_sha256,
        expected_model_content_sha256=args.model_sha256,
        variant_id=ABLATION_D,
        current_source=CurrentSourceDeclarationV1(
            stage5_content_sha256=args.stage5_content_sha256,
            component_id=args.component_id,
            declared_held_out=True,
        ),
    )
    try:
        with SimulatorBridgeDynamicV4(args.bridge, cwd=args.simulator_root) as bridge:
            session = bind_responsive_incantagos_development_session_v1(
                bridge=bridge, case=case, loaded_model=loaded,
                simulator_seed=args.simulator_seed, teammate_seed=args.teammate_seed,
                pair_id=f"incantagos-v4-development-seed-{args.simulator_seed}",
                branch_id="responsive-program-replay",
                candidate_suffix_id="current-state-program",
            )
            adapter = session.adapter
            # Binding already planned and armed the earliest wake; do not resample it.
            deadlines = list(adapter._deadline_by_actor.values())
            discarded = list(adapter.horizon_discard_evidence())
            all_rows = deadlines + discarded
            legacy_levels = Counter()
            fixed_levels = Counter()
            initial_alive = Counter()
            for guid in sorted(case.teammate_player_guids):
                actor = adapter.runtime.actors[guid].metadata
                state = adapter.runtime.snapshot_for_actor(guid)
                targets = state["target_state"]
                initial_alive[len(targets["alive_target_guids"])] += 1
                old_contexts = [
                    (key[0], "WAVE_START", *key[1:])
                    for key in _context_keys(actor, state, ABLATION_D)
                ]
                old_level = next(
                    (key[0] for key in old_contexts
                     if (distribution := loaded.model._distribution("delay_counts", key))
                     is not None and distribution[0] >= loaded.model.minimums[key[0]]),
                    "NO_SUPPORT",
                )
                legacy_levels[old_level] += 1
            for row in all_rows:
                fixed_levels[row["delay_sample"]["context_level"]] += 1
            result = {
                "schema": "development_d900_initial_delay_projection_smoke/v1",
                "status": "COMPLETE_FIRST_WAKE_CONTEXT_ONLY",
                "comparison_authorized": False,
                "simulator_seed": args.simulator_seed,
                "teammate_seed": args.teammate_seed,
                "wave_id": args.wave_id,
                "model_result_sha": args.result_sha256,
                "model_sha": args.model_sha256,
                "dynamic_config_content_sha256": case.dynamic_config.content_sha256,
                "case_request_sha256": hashlib.sha256(
                    json.dumps(case.request, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest(),
                "metadata_artifact": str(args.metadata),
                "exact_build_artifact": str(args.exact_build),
                "attackability_mode": case.receipt.get("fixed_attackability_mode"),
                "attackability_events": [asdict(row) for row in case.dynamic_config.attackability_events],
                "effective_armor_events": [asdict(row) for row in case.dynamic_config.effective_armor_events],
                "teammate_count": len(case.teammate_player_guids),
                "initial_prefix_alive_target_count_by_actor": dict(sorted(initial_alive.items())),
                "legacy_context_levels_if_unprojected": dict(sorted(legacy_levels.items())),
                "projected_context_levels": dict(sorted(fixed_levels.items())),
                "planned_deadline_count": len(deadlines),
                "horizon_discard_count": len(discarded),
                "initial_wake": session.receipt.get("initial_wake"),
                "actor_757d23_deadline": next(
                    (row for row in all_rows if "757D23" in row["actor_guid"].upper()),
                    None,
                ),
                "emission_count": 0,
                "target_registry_unchanged": True,
                "case_initial_health": [
                    {"target_index": row.target_index, "maximum_health": row.maximum_health,
                     "current_health": row.current_health}
                    for row in case.dynamic_config.target_health
                ],
                "load_state_targets_before_zero_wakes": session.load_result.state[
                    "dynamic_target_semantics"
                ]["targets"],
            }
            zero_events = []
            while bridge.state().get("wake_ready") is not None:
                if len(zero_events) >= 100:
                    raise RuntimeError("unexpected number of zero-time teammate events")
                step = adapter.emit_global_ready_and_rearm()
                emitted = step["emitted"]
                receipt = emitted["wire_receipt"]
                zero_events.append({
                    "actor_guid": receipt["actor_guid"],
                    "time_ms": receipt["time_ms"],
                    "event_type": receipt["event_type"],
                    "target_index": receipt["target_index"],
                    "spell_id": emitted["sampled_emission"].get("spell_id"),
                    "applied_damage": receipt["applied_damage"],
                })
            result["zero_time_events"] = zero_events
            result["emission_count"] = len(zero_events)
            result["state_targets_after_zero_wakes"] = bridge.state()[
                "dynamic_target_semantics"
            ]["targets"]
    finally:
        loaded.model.close()
    if sum(fixed_levels.values()) != len(case.teammate_player_guids):
        raise RuntimeError("first-wake samples do not cover the teammate roster")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code-root", type=Path)
    for name in ("metadata", "stage5", "exact-build", "frozen-dispatch", "runtime-store",
                 "bridge", "simulator-root", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("component-id", "stage5-content-sha256", "partition-compressed-file-sha256",
                 "result-sha256", "model-sha256", "wave-id", "attackability-mode"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--simulator-seed", type=int, required=True)
    parser.add_argument("--teammate-seed", type=int, required=True)
    args = parser.parse_args()
    result = run(args)
    print(json.dumps({key: result[key] for key in (
        "status", "teammate_count", "initial_prefix_alive_target_count_by_actor",
        "legacy_context_levels_if_unprojected", "projected_context_levels",
        "planned_deadline_count", "horizon_discard_count", "emission_count",
    )}, ensure_ascii=False))


if __name__ == "__main__":
    main()
