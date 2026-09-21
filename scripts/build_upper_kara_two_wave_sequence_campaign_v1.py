"""Build the frozen v7 heterogeneous two-wave sequence campaign.

The output is a create-only local JSON contract.  This script does not launch
workers or contact the remote scheduler.  Arrival delay is balanced as an
environment nuisance stratum and is not part of the policy search spec.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.development_two_wave_build_panel_v1 import BUILD_IDS
from o2o_dps.development_two_wave_sequence_candidates_v1 import (
    DevelopmentTwoWaveFreshSeedProtocolV1,
)
from o2o_dps.sim_bridge import ActionRef
from o2o_dps.upper_kara_burst_package_search_v1 import BURST_LOADOUT_IDS_V1
from o2o_dps.upper_kara_causal_program_remote_contract_v1 import (
    HETEROGENEOUS_TWO_WAVE_MIN_MAX_DECISIONS_V7,
    HETEROGENEOUS_TWO_WAVE_PROPOSAL_GUIDE_IDS_V7,
    ContinuousTwoWaveRemoteCampaignV1,
    HeterogeneousTwoWaveSequenceSearchV7,
    campaign_from_dict_v1,
)
from o2o_dps.upper_kara_causal_program_search_v1 import (
    UpperKaraCausalProgramGenerationConfigV1,
)
from o2o_dps.upper_kara_two_wave_train_eval_v1 import TwoWaveExampleV1


JSONMap = dict[str, Any]
DEFAULT_CAMPAIGN_ID_V1 = "upper-kara-heterogeneous-two-wave-sequence-v7-256x256"
DEFAULT_DEATH_WISH_ACTION_V1 = ActionRef(spell_id=12_328)
DEFAULT_RESOURCE_ID_V1 = "warrior.death_wish"
DEFAULT_WAVE_ONE_MIN_TARGET_HP_PCT_V1 = 50.0
DEFAULT_WAVE_ONE_MIN_ESTIMATED_REMAINING_MS_V1 = 4_500
DEFAULT_PULL_TIME_MS_V1 = 3_000


def _examples_v1(
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


def build_upper_kara_two_wave_sequence_campaign_v1(
    *,
    campaign_id: str = DEFAULT_CAMPAIGN_ID_V1,
    build_id: str = BUILD_IDS[0],
    generation_config: UpperKaraCausalProgramGenerationConfigV1 | None = None,
    max_decisions: int = HETEROGENEOUS_TWO_WAVE_MIN_MAX_DECISIONS_V7,
    pull_time_ms: int = DEFAULT_PULL_TIME_MS_V1,
    long_cooldown_action: ActionRef = DEFAULT_DEATH_WISH_ACTION_V1,
    resource_id: str = DEFAULT_RESOURCE_ID_V1,
    wave_one_min_target_hp_pct: float = (
        DEFAULT_WAVE_ONE_MIN_TARGET_HP_PCT_V1
    ),
    wave_one_min_estimated_remaining_ms: int = (
        DEFAULT_WAVE_ONE_MIN_ESTIMATED_REMAINING_MS_V1
    ),
) -> ContinuousTwoWaveRemoteCampaignV1:
    """Return the predeclared 256-by-256 v7 campaign contract."""

    fresh = DevelopmentTwoWaveFreshSeedProtocolV1()
    arrivals = fresh.environment_arrival_nuisance_schedule_ms
    spec = HeterogeneousTwoWaveSequenceSearchV7(
        long_cooldown_action=long_cooldown_action,
        resource_id=resource_id,
        wave_one_min_target_hp_pct=wave_one_min_target_hp_pct,
        wave_one_min_estimated_remaining_ms=(
            wave_one_min_estimated_remaining_ms
        ),
        proposal_guide_ids=(
            HETEROGENEOUS_TWO_WAVE_PROPOSAL_GUIDE_IDS_V7
        ),
        fresh_seed_contract=fresh,
    )
    campaign = ContinuousTwoWaveRemoteCampaignV1(
        campaign_id=campaign_id,
        build_id=build_id,
        train_examples=_examples_v1(fresh.train_seeds, arrivals),
        evaluation_examples=_examples_v1(fresh.evaluation_seeds, arrivals),
        generation_config=(
            UpperKaraCausalProgramGenerationConfigV1()
            if generation_config is None
            else generation_config
        ),
        loadout_ids=BURST_LOADOUT_IDS_V1,
        seed_shard_count=fresh.seed_count,
        pull_time_ms=pull_time_ms,
        max_decisions=max_decisions,
        search_spec=spec,
    )
    # Force the same strict parser used by remote consumers before publishing.
    if campaign_from_dict_v1(campaign.to_dict()) != campaign:
        raise AssertionError("v7 campaign failed canonical roundtrip")
    return campaign


def _json_bytes_v1(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def write_json_create_only_v1(
    path: str | Path, value: Mapping[str, Any]
) -> Path:
    """Atomically publish one JSON file without replacing an existing path."""

    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_json_bytes_v1(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination.resolve()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--campaign-id", default=DEFAULT_CAMPAIGN_ID_V1)
    parser.add_argument("--build-id", choices=BUILD_IDS, default=BUILD_IDS[0])
    parser.add_argument(
        "--max-decisions",
        type=int,
        default=HETEROGENEOUS_TWO_WAVE_MIN_MAX_DECISIONS_V7,
    )
    parser.add_argument(
        "--pull-time-ms", type=int, default=DEFAULT_PULL_TIME_MS_V1
    )
    parser.add_argument(
        "--long-cooldown-spell-id",
        type=int,
        default=DEFAULT_DEATH_WISH_ACTION_V1.spell_id,
    )
    parser.add_argument("--resource-id", default=DEFAULT_RESOURCE_ID_V1)
    parser.add_argument(
        "--wave-one-min-target-hp-pct",
        type=float,
        default=DEFAULT_WAVE_ONE_MIN_TARGET_HP_PCT_V1,
    )
    parser.add_argument(
        "--wave-one-min-estimated-remaining-ms",
        type=int,
        default=DEFAULT_WAVE_ONE_MIN_ESTIMATED_REMAINING_MS_V1,
    )
    args = parser.parse_args()
    campaign = build_upper_kara_two_wave_sequence_campaign_v1(
        campaign_id=args.campaign_id,
        build_id=args.build_id,
        max_decisions=args.max_decisions,
        pull_time_ms=args.pull_time_ms,
        long_cooldown_action=ActionRef(
            spell_id=args.long_cooldown_spell_id
        ),
        resource_id=args.resource_id,
        wave_one_min_target_hp_pct=args.wave_one_min_target_hp_pct,
        wave_one_min_estimated_remaining_ms=(
            args.wave_one_min_estimated_remaining_ms
        ),
    )
    destination = write_json_create_only_v1(args.output, campaign.to_dict())
    print(destination)


if __name__ == "__main__":
    main()


__all__ = (
    "DEFAULT_CAMPAIGN_ID_V1",
    "DEFAULT_DEATH_WISH_ACTION_V1",
    "DEFAULT_RESOURCE_ID_V1",
    "build_upper_kara_two_wave_sequence_campaign_v1",
    "write_json_create_only_v1",
)
