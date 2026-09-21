"""Run one shard of the frozen d900 winner with all-seed terminal endpoints."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from o2o_dps.offline_wave_d3_frozen_heldout_v1 import CONTROLLER_IDS
from o2o_dps.offline_wave_d4_all_seed_endpoint_v1 import (
    EVIDENCE_MODES,
    build_all_seed_endpoint_contract_v1,
    build_all_seed_endpoint_row_v1,
    extract_terminal_endpoint_v1,
)
from o2o_dps.wave_action_sequence_search_v1 import ReplayStatusV1
from scripts.development_offline_wave_policy_d900_d3_frozen_heldout_v1 import (
    _seed_pairs,
    build_argument_parser_v1,
    run as run_d3_frozen_panel_v1,
)
from scripts.development_responsive_upper_kara_trash_smoke_v1 import build_case


OUTPUT_SCHEMA = "development_offline_wave_policy_d900_d4_all_seed_shard/v1"


def run(args: argparse.Namespace) -> dict[str, Any]:
    frozen_source = json.loads(
        args.frozen_d3_result.read_text(encoding="utf-8")
    )
    prior_expansion = json.loads(
        args.prior_frozen_expansion_result.read_text(encoding="utf-8")
    )
    campaign_pairs = _seed_pairs(
        args.campaign_first_seed, args.campaign_seed_count
    )
    executed_pairs = _seed_pairs(args.heldout_seed, args.heldout_seed_count)
    if not set(executed_pairs).issubset(set(campaign_pairs)):
        raise ValueError("executed shard is outside the frozen endpoint campaign")

    _fixed, case = build_case(args)
    horizon_ms = case.dynamic_config.idle_advance_horizon_ms
    required_target_indexes = tuple(range(len(case.dynamic_config.target_health)))
    endpoint_contract = build_all_seed_endpoint_contract_v1(
        frozen_d3_source=frozen_source,
        prior_expansion_source=prior_expansion,
        seed_pairs=campaign_pairs,
        campaign_id=args.endpoint_campaign_id,
        horizon_ms=horizon_ms,
        evidence_mode=args.evidence_mode,
    )
    candidate_id = endpoint_contract["frozen_candidate"]["candidate_id"]

    def enrich_runtime_row(**values: Any) -> dict[str, Any]:
        outcome = values["outcome"]
        replay_status = (
            outcome.status.value
            if isinstance(outcome.status, ReplayStatusV1)
            else str(outcome.status)
        )
        terminal = extract_terminal_endpoint_v1(
            outcome,
            required_target_indexes=required_target_indexes,
            horizon_ms=horizon_ms,
        )
        endpoint_row = build_all_seed_endpoint_row_v1(
            candidate_id=candidate_id,
            controller_id=values["controller_id"],
            simulator_seed=values["simulator_seed"],
            teammate_seed=values["teammate_seed"],
            replay_status=replay_status,
            terminal_endpoint=terminal,
            domain_fallback_calls=values["domain_fallback_calls"],
        )
        return {"all_seed_endpoint": endpoint_row}

    source_panel = run_d3_frozen_panel_v1(
        args, runtime_row_enricher=enrich_runtime_row
    )
    if source_panel["contract"]["frozen_candidate"] != endpoint_contract[
        "frozen_candidate"
    ]:
        raise RuntimeError("source panel and endpoint contract freeze different winners")
    source_pairs = tuple(
        (row["simulator_seed"], row["teammate_seed"])
        for row in source_panel["contract"]["heldout_seed_pairs"]
    )
    if source_pairs != executed_pairs:
        raise RuntimeError("source panel executed a different shard seed set")
    endpoint_rows = [row["all_seed_endpoint"] for row in source_panel["runtime_rows"]]
    expected_row_count = len(executed_pairs) * len(CONTROLLER_IDS)
    if len(endpoint_rows) != expected_row_count:
        raise RuntimeError("endpoint shard lacks its exact five-controller rows")

    all_eligible = all(row["all_seed_endpoint_eligible"] for row in endpoint_rows)
    return {
        "schema": OUTPUT_SCHEMA,
        "status": (
            "ALL_SEED_ENDPOINT_SHARD_COMPLETE"
            if all_eligible
            else "ALL_SEED_ENDPOINT_SHARD_INVALID"
        ),
        "contract": endpoint_contract,
        "executed_seed_pairs": [
            {"simulator_seed": simulator, "teammate_seed": teammate}
            for simulator, teammate in executed_pairs
        ],
        "endpoint_rows": endpoint_rows,
        "source_runtime_rows": source_panel["runtime_rows"],
        "source_panel_receipt": source_panel["receipt"],
        "frozen_source_binding": source_panel["frozen_source_binding"],
        "request_sha256": source_panel["request_sha256"],
        "dynamic_config_sha256": source_panel["dynamic_config_sha256"],
        "evaluation_build_ref": source_panel["evaluation_build_ref"],
        "target_rule_id": source_panel["target_rule_id"],
        "required_target_indexes": list(required_target_indexes),
        "fixed_horizon_ms": horizon_ms,
        "parallel_lane_workers": source_panel["parallel_lane_workers"],
        "contracts": {
            "one_seed_per_remote_process": args.heldout_seed_count == 1,
            "same_frozen_winner_as_d3": True,
            "all_seed_endpoints_frozen_before_execution": True,
            "incomplete_waves_are_valid_terminal_observations": True,
            "incomplete_damage_imputed_as_zero": False,
            "incomplete_dps_imputed_as_zero": False,
            "responsive_team_is_model_not_historical_replay": True,
            "real_environment_comparison_authorized": False,
            "deployment_authorized": False,
        },
    }


def build_argument_parser() -> argparse.ArgumentParser:
    parser = build_argument_parser_v1(description=__doc__)
    parser.add_argument(
        "--prior-frozen-expansion-result", type=Path, required=True
    )
    parser.add_argument("--endpoint-campaign-id", required=True)
    parser.add_argument("--campaign-first-seed", type=int, required=True)
    parser.add_argument("--campaign-seed-count", type=int, required=True)
    parser.add_argument("--evidence-mode", choices=EVIDENCE_MODES, required=True)
    return parser


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()
    if not 1 <= args.campaign_seed_count <= 256:
        parser.error("campaign-seed-count must be in 1..256")
    if args.heldout_seed_count != 1:
        parser.error("D4 remote shards require exactly one held-out seed")
    print(json.dumps(run(args), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
