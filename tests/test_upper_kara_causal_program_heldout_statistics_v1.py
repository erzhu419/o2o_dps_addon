from __future__ import annotations

import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from o2o_dps.causal_action_program_v1 import ProgramOriginV1
from o2o_dps.upper_kara_burst_package_search_v1 import BURST_LOADOUT_IDS_V1
from o2o_dps.upper_kara_cat_residual_overlay_search_v1 import (
    cat_zero_residual_program_v1,
)
from o2o_dps.upper_kara_causal_program_remote_contract_v1 import (
    TERMINAL_COMPLETE,
    ContinuousTwoWaveRemoteCampaignV1,
    assign_evaluation_shards_v1,
)
from o2o_dps.upper_kara_causal_program_remote_worker_v1 import (
    EVAL_TERMINAL_SCHEMA,
    FREEZE_TERMINAL_SCHEMA,
    _campaign_contract_v1,
    evaluation_terminal_name_v1,
    summarize_evaluation_v1,
)
from o2o_dps.upper_kara_causal_program_search_v1 import (
    UpperKaraCausalProgramGenerationConfigV1,
)
from o2o_dps.upper_kara_two_wave_train_eval_v1 import TwoWaveExampleV1


BASELINE_IDS = ("baseline.a", "baseline.b", "baseline.c")


def _campaign() -> ContinuousTwoWaveRemoteCampaignV1:
    return ContinuousTwoWaveRemoteCampaignV1(
        campaign_id="heldout-statistics-smoke",
        build_id="clean_dual_weapon_probe",
        train_examples=tuple(
            TwoWaveExampleV1(seed, arrival)
            for seed, arrival in zip(
                range(1, 7),
                (0, 0, 1_000, 1_000, 2_000, 2_000),
                strict=True,
            )
        ),
        evaluation_examples=tuple(
            TwoWaveExampleV1(seed, arrival)
            for seed, arrival in zip(
                range(201, 207),
                (0, 0, 0, 1_000, 1_000, 1_000),
                strict=True,
            )
        ),
        generation_config=UpperKaraCausalProgramGenerationConfigV1(
            max_programs=1
        ),
        seed_shard_count=2,
    )


def _write_panel(root: Path) -> tuple[Path, Path, Path, dict[str, Path]]:
    campaign = _campaign()
    campaign_path = root / "campaign.json"
    campaign_path.write_text(
        json.dumps(campaign.to_dict()), encoding="utf-8"
    )
    loadout_id = BURST_LOADOUT_IDS_V1[0]
    program = cat_zero_residual_program_v1(loadout_id)
    frozen_path = root / "frozen.json"
    frozen_path.write_text(
        json.dumps(
            {
                "schema": FREEZE_TERMINAL_SCHEMA,
                "terminal_status": TERMINAL_COMPLETE,
                "campaign_id": campaign.campaign_id,
                "build_id": campaign.build_id,
                "campaign_contract": _campaign_contract_v1(campaign),
                "winner": {
                    "loadout_id": loadout_id,
                    "program_ref": program.program_id,
                    "program_id": program.program_id,
                    "program_key": program.program_key(),
                    "program_origin": ProgramOriginV1.SEARCHED.value,
                },
                "frozen_program": program.to_dict(),
            }
        ),
        encoding="utf-8",
    )

    damages = {
        201: {"frozen_candidate": 110, "baseline.a": 100, "baseline.b": 105, "baseline.c": 120},
        202: {"frozen_candidate": 90, "baseline.a": 100, "baseline.b": 85, "baseline.c": 100},
        203: {"frozen_candidate": 100, "baseline.a": 100, "baseline.b": 95, "baseline.c": 110},
        204: {"frozen_candidate": 200, "baseline.a": 180, "baseline.b": 200, "baseline.c": 190},
        205: {"frozen_candidate": 210, "baseline.a": 220, "baseline.b": 210, "baseline.c": 190},
        206: {"frozen_candidate": 220, "baseline.a": 220, "baseline.b": 220, "baseline.c": 190},
    }
    evaluation_root = root / "eval"
    evaluation_root.mkdir()
    paths: dict[str, Path] = {}
    for shard in assign_evaluation_shards_v1(campaign):
        lanes = []
        for example in shard.examples:
            for policy_id in ("frozen_candidate", *BASELINE_IDS):
                damage = float(damages[example.seed][policy_id])
                lanes.append(
                    {
                        "master_seed": example.seed,
                        "simulator_seed": example.seed + 10_000,
                        "policy_id": policy_id,
                        "status": TERMINAL_COMPLETE,
                        "own_effective_damage": damage,
                        "own_effective_dps": damage / 10.0,
                        "ttk_ms": 10_000.0,
                        "first_wave_arrival_ms": example.first_wave_arrival_ms,
                    }
                )
        path = evaluation_root / evaluation_terminal_name_v1(shard)
        path.write_text(
            json.dumps(
                {
                    "schema": EVAL_TERMINAL_SCHEMA,
                    "terminal_status": TERMINAL_COMPLETE,
                    "campaign_id": campaign.campaign_id,
                    "build_id": campaign.build_id,
                    "campaign_contract": _campaign_contract_v1(campaign),
                    "selected_loadout_id": loadout_id,
                    "frozen_program_id": program.program_id,
                    "frozen_program_key": program.program_key(),
                    "frozen_program_origin": ProgramOriginV1.SEARCHED.value,
                    "seed_shard_index": shard.seed_shard_index,
                    "examples": [row.to_dict() for row in shard.examples],
                    "baseline_policy_ids": list(BASELINE_IDS),
                    "lanes": lanes,
                }
            ),
            encoding="utf-8",
        )
        paths[shard.work_id] = path
    return campaign_path, frozen_path, evaluation_root, paths


class HeldoutPairedStatisticsTests(unittest.TestCase):
    def test_summary_adds_paired_intervals_and_predeclared_arrival_strata(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign_path, frozen_path, evaluation_root, _ = _write_panel(root)
            summary = summarize_evaluation_v1(
                campaign_path=campaign_path,
                frozen_path=frozen_path,
                evaluation_root=evaluation_root,
                output_path=root / "summary.json",
            )

            metric = summary["metric"]
            detailed = metric[
                "paired_candidate_minus_baseline_damage_statistics"
            ]
            self.assertEqual(6, detailed["baseline.a"]["pair_count"])
            self.assertEqual(
                metric["paired_candidate_minus_baseline_mean_damage"][
                    "baseline.a"
                ],
                detailed["baseline.a"]["mean_damage_delta"],
            )
            self.assertEqual(
                {"wins": 2, "ties": 2, "losses": 2},
                detailed["baseline.a"]["win_tie_loss"],
            )

            strata = {
                row["first_wave_arrival_ms"]: row
                for row in metric["by_first_wave_arrival_ms"]
            }
            self.assertEqual({0, 1_000}, set(strata))
            zero = strata[0]
            self.assertEqual(3, zero["seed_count"])
            self.assertEqual(
                100.0,
                zero["policy_means"]["frozen_candidate"][
                    "mean_own_effective_damage"
                ],
            )
            zero_a = zero[
                "paired_candidate_minus_baseline_damage_statistics"
            ]["baseline.a"]
            self.assertEqual(0.0, zero_a["mean_damage_delta"])
            self.assertAlmostEqual(10.0 / math.sqrt(3), zero_a["standard_error"])
            half_width = 1.9599639845400536 * 10.0 / math.sqrt(3)
            self.assertAlmostEqual(
                -half_width,
                zero_a["normal_confidence_interval"]["lower_bound"],
            )
            self.assertAlmostEqual(
                half_width,
                zero_a["normal_confidence_interval"]["upper_bound"],
            )
            self.assertEqual(
                {"wins": 1, "ties": 1, "losses": 1},
                zero_a["win_tie_loss"],
            )
            one_thousand_c = strata[1_000][
                "paired_candidate_minus_baseline_damage_statistics"
            ]["baseline.c"]
            self.assertEqual(20.0, one_thousand_c["mean_damage_delta"])
            self.assertEqual(
                {"wins": 3, "ties": 0, "losses": 0},
                one_thousand_c["win_tie_loss"],
            )

    def test_summary_rejects_lane_arrival_not_bound_to_campaign_seed(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign_path, frozen_path, evaluation_root, paths = _write_panel(root)
            path = next(iter(paths.values()))
            terminal = json.loads(path.read_text(encoding="utf-8"))
            terminal["lanes"][0]["first_wave_arrival_ms"] = 9_999
            path.write_text(json.dumps(terminal), encoding="utf-8")

            with self.assertRaisesRegex(
                ValueError, "arrival differs from the predeclared campaign"
            ):
                summarize_evaluation_v1(
                    campaign_path=campaign_path,
                    frozen_path=frozen_path,
                    evaluation_root=evaluation_root,
                    output_path=root / "summary.json",
                )


if __name__ == "__main__":
    unittest.main()
