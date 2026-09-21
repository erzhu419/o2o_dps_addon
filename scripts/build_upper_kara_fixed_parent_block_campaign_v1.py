"""Build a fresh fixed-parent queue/GCD search campaign from a v4 freeze."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.causal_action_program_v1 import (
    ImportedFallbackOverlaySelectorV1,
    causal_action_program_from_dict_v1,
)
from o2o_dps.upper_kara_causal_program_remote_contract_v1 import (
    ContinuousTwoWaveRemoteCampaignV1,
    FixedParentQueueGcdBlockSearchV1,
    load_continuous_two_wave_remote_campaign_v1,
)
from o2o_dps.upper_kara_two_wave_train_eval_v1 import TwoWaveExampleV1


ARRIVALS_MS = (0, 1_000, 3_000, 5_000, 7_000, 9_000)


def _examples(start: int, count: int) -> tuple[TwoWaveExampleV1, ...]:
    if start <= 0 or count <= 0:
        raise ValueError("seed start and count must be positive")
    return tuple(
        TwoWaveExampleV1(start + index, ARRIVALS_MS[index % len(ARRIVALS_MS)])
        for index in range(count)
    )


def build_campaign_v1(
    *,
    base_campaign: str | Path,
    frozen: str | Path,
    campaign_id: str,
    train_seed_start: int,
    evaluation_seed_start: int,
    seed_count: int,
    max_block_programs: int,
    seed_shard_count: int,
) -> ContinuousTwoWaveRemoteCampaignV1:
    base = load_continuous_two_wave_remote_campaign_v1(base_campaign)
    frozen_value = json.loads(
        Path(frozen).expanduser().resolve(strict=True).read_text(
            encoding="utf-8-sig"
        )
    )
    if (
        not isinstance(frozen_value, dict)
        or frozen_value.get("terminal_status") != "COMPLETE"
        or not isinstance(frozen_value.get("winner"), dict)
    ):
        raise ValueError("frozen input is not a complete winner artifact")
    winner = frozen_value["winner"]
    parent = causal_action_program_from_dict_v1(
        frozen_value.get("frozen_program")
    )
    if (
        winner.get("program_id") != parent.program_id
        or winner.get("program_key") != parent.program_key()
        or not isinstance(parent.selector, ImportedFallbackOverlaySelectorV1)
    ):
        raise ValueError("frozen parent identity or selector is inconsistent")
    loadout_id = winner.get("loadout_id")
    spec = FixedParentQueueGcdBlockSearchV1(
        parent_loadout_id=loadout_id,
        parent_program=parent,
        max_block_programs=max_block_programs,
    )
    return ContinuousTwoWaveRemoteCampaignV1(
        campaign_id=campaign_id,
        build_id=base.build_id,
        train_examples=_examples(train_seed_start, seed_count),
        evaluation_examples=_examples(evaluation_seed_start, seed_count),
        generation_config=base.generation_config,
        loadout_ids=(loadout_id,),
        seed_shard_count=seed_shard_count,
        pull_time_ms=base.pull_time_ms,
        max_decisions=base.max_decisions,
        search_spec=spec,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-campaign", type=Path, required=True)
    parser.add_argument("--frozen", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--train-seed-start", type=int, required=True)
    parser.add_argument("--evaluation-seed-start", type=int, required=True)
    parser.add_argument("--seed-count", type=int, default=256)
    parser.add_argument("--max-block-programs", type=int, default=384)
    parser.add_argument("--seed-shard-count", type=int, default=256)
    args = parser.parse_args()
    campaign = build_campaign_v1(
        base_campaign=args.base_campaign,
        frozen=args.frozen,
        campaign_id=args.campaign_id,
        train_seed_start=args.train_seed_start,
        evaluation_seed_start=args.evaluation_seed_start,
        seed_count=args.seed_count,
        max_block_programs=args.max_block_programs,
        seed_shard_count=args.seed_shard_count,
    )
    wire = campaign.to_dict()
    # The contract is deterministically reconstructed by the loader.  Keeping
    # it out of the input avoids duplicating both 256-row panels in one file.
    wire.pop("contract")
    destination = args.output.expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(wire, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(destination.resolve())


if __name__ == "__main__":
    main()
