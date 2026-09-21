"""Build the frozen V8 Cat-relative action-plan residual campaign.

The output is a create-only local JSON contract.  This script does not launch
workers or contact the remote scheduler.  The proposal teacher receives only
the balanced 128-seed proposal cohort; selection and held-out seeds remain
excluded from teacher generation.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.development_two_wave_build_panel_v1 import BUILD_IDS
from o2o_dps.upper_kara_burst_package_search_v1 import BURST_LOADOUT_IDS_V1
from o2o_dps.upper_kara_causal_program_remote_contract_v1 import (
    CAT_ACTION_PLAN_FRESH_SEED_CONTRACT_V8,
    CAT_ACTION_PLAN_RESIDUAL_SEQUENCE_MIN_MAX_DECISIONS_V8,
    CatActionPlanResidualSequenceSearchV8,
    ContinuousTwoWaveRemoteCampaignV1,
    campaign_from_dict_v1,
)
from o2o_dps.upper_kara_causal_program_search_v1 import (
    UpperKaraCausalProgramGenerationConfigV1,
)
from o2o_dps.upper_kara_two_wave_train_eval_v1 import TwoWaveExampleV1
from scripts.build_upper_kara_two_wave_sequence_campaign_v1 import (
    write_json_create_only_v1,
)


DEFAULT_CAMPAIGN_ID_V8 = "upper-kara-cat-action-plan-residual-v8-256x256"
DEFAULT_PULL_TIME_MS_V8 = 3_000


def _examples_v8(
    seeds: tuple[int, ...], arrival_schedule_ms: tuple[int, ...]
) -> tuple[TwoWaveExampleV1, ...]:
    return tuple(
        TwoWaveExampleV1(
            seed=seed,
            first_wave_arrival_ms=(
                arrival_schedule_ms[index % len(arrival_schedule_ms)]
            ),
        )
        for index, seed in enumerate(seeds)
    )


def build_upper_kara_cat_action_plan_residual_campaign_v8(
    *,
    campaign_id: str = DEFAULT_CAMPAIGN_ID_V8,
    build_id: str = BUILD_IDS[0],
    generation_config: UpperKaraCausalProgramGenerationConfigV1 | None = None,
    max_decisions: int = (
        CAT_ACTION_PLAN_RESIDUAL_SEQUENCE_MIN_MAX_DECISIONS_V8
    ),
    pull_time_ms: int = DEFAULT_PULL_TIME_MS_V8,
) -> ContinuousTwoWaveRemoteCampaignV1:
    """Return the predeclared 256-by-256 V8 campaign contract."""

    fresh = CAT_ACTION_PLAN_FRESH_SEED_CONTRACT_V8
    arrivals = fresh.environment_arrival_nuisance_schedule_ms
    campaign = ContinuousTwoWaveRemoteCampaignV1(
        campaign_id=campaign_id,
        build_id=build_id,
        train_examples=_examples_v8(fresh.train_seeds, arrivals),
        evaluation_examples=_examples_v8(fresh.evaluation_seeds, arrivals),
        generation_config=(
            UpperKaraCausalProgramGenerationConfigV1()
            if generation_config is None
            else generation_config
        ),
        loadout_ids=BURST_LOADOUT_IDS_V1,
        seed_shard_count=fresh.seed_count,
        pull_time_ms=pull_time_ms,
        max_decisions=max_decisions,
        search_spec=CatActionPlanResidualSequenceSearchV8(),
    )
    if campaign_from_dict_v1(campaign.to_dict()) != campaign:
        raise AssertionError("V8 campaign failed canonical roundtrip")
    return campaign


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--campaign-id", default=DEFAULT_CAMPAIGN_ID_V8)
    parser.add_argument("--build-id", choices=BUILD_IDS, default=BUILD_IDS[0])
    parser.add_argument(
        "--max-decisions",
        type=int,
        default=CAT_ACTION_PLAN_RESIDUAL_SEQUENCE_MIN_MAX_DECISIONS_V8,
    )
    parser.add_argument(
        "--pull-time-ms", type=int, default=DEFAULT_PULL_TIME_MS_V8
    )
    args = parser.parse_args()
    campaign = build_upper_kara_cat_action_plan_residual_campaign_v8(
        campaign_id=args.campaign_id,
        build_id=args.build_id,
        max_decisions=args.max_decisions,
        pull_time_ms=args.pull_time_ms,
    )
    destination = write_json_create_only_v1(args.output, campaign.to_dict())
    print(destination)


if __name__ == "__main__":
    main()


__all__ = (
    "DEFAULT_CAMPAIGN_ID_V8",
    "DEFAULT_PULL_TIME_MS_V8",
    "build_upper_kara_cat_action_plan_residual_campaign_v8",
)
