from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps.causal_action_program_v1 import (
    CausalActionProgramV1,
    ImportedFallbackOverlaySelectorV1,
    ImportedReactiveBurstQueueGcdBlockSelectorV1,
    ProgramOriginV1,
)
from o2o_dps.upper_kara_cat_residual_overlay_search_v1 import (
    cat_zero_residual_program_v1,
)
from o2o_dps.upper_kara_causal_program_remote_contract_v1 import (
    ContinuousTwoWaveRemoteCampaignV1,
    FixedParentQueueGcdBlockSearchV1,
    assign_training_shards_v1,
)
from o2o_dps.upper_kara_causal_program_remote_worker_v1 import (
    SUMMARY_TERMINAL_SCHEMA,
    TRAIN_TERMINAL_SCHEMA,
    _campaign_contract_v1,
    train_terminal_name_v1,
)
from o2o_dps.upper_kara_causal_program_search_v1 import (
    UpperKaraCausalProgramGenerationConfigV1,
)
from o2o_dps.upper_kara_first_wave_stratified_candidate_diagnostics_v1 import (
    SCHEMA,
    reduce_first_wave_stratified_candidate_artifacts_v1,
    reduce_first_wave_stratified_candidate_diagnostics_v1,
)
from o2o_dps.upper_kara_two_wave_train_eval_v1 import TwoWaveExampleV1


LOADOUT = "contra_turtle_burst__mighty_rage"


def _programs() -> tuple[CausalActionProgramV1, CausalActionProgramV1]:
    zero = cat_zero_residual_program_v1(LOADOUT)
    parent = zero.selector
    if not isinstance(parent, ImportedFallbackOverlaySelectorV1):
        raise AssertionError("fixture Cat zero changed selector type")
    candidate = CausalActionProgramV1(
        program_id="fixture-sparse-candidate",
        selector=ImportedReactiveBurstQueueGcdBlockSelectorV1(
            terminal_alternatives=parent.terminal_alternatives,
            imported_fallback=parent.imported_fallback,
            block_alternatives=(),
            off_gcd_insertions=parent.off_gcd_insertions,
            insertion_order=parent.insertion_order,
            insertion_position=parent.insertion_position,
        ),
        origin=ProgramOriginV1.SEARCHED,
        source_refs=(
            *zero.source_refs,
            "sparse-family:GCD_PREFIX",
            f"parent-program:{zero.program_id}",
        ),
    )
    return zero, candidate


def _campaign(zero: CausalActionProgramV1) -> ContinuousTwoWaveRemoteCampaignV1:
    arrivals = (0, 0, 3_000, 3_000, 0, 0, 3_000, 3_000)
    return ContinuousTwoWaveRemoteCampaignV1(
        campaign_id="stratified-diagnostic-fixture",
        build_id="live_bonereaver",
        train_examples=tuple(
            TwoWaveExampleV1(seed, arrival)
            for seed, arrival in zip(range(1, 9), arrivals, strict=True)
        ),
        evaluation_examples=tuple(
            TwoWaveExampleV1(seed, arrival)
            for seed, arrival in zip(range(101, 109), arrivals, strict=True)
        ),
        generation_config=UpperKaraCausalProgramGenerationConfigV1(
            max_programs=2
        ),
        loadout_ids=(LOADOUT,),
        seed_shard_count=8,
        search_spec=FixedParentQueueGcdBlockSearchV1(
            parent_loadout_id=LOADOUT,
            parent_program=zero,
            max_block_programs=2,
        ),
    )


def _receipt(program: CausalActionProgramV1) -> dict:
    return {
        "program_ref": program.program_id,
        "program_id": program.program_id,
        "program_key": program.program_key(),
        "program_origin": program.origin.value,
        "proposal_guide_ids": [],
        "program": program.to_dict(),
    }


def _candidate_delta(seed: int) -> float:
    return {
        1: 10.0,
        2: 5.0,
        3: 10.0,
        4: 10.0,
        5: 10.0,
        6: 5.0,
        7: 10.0,
        8: -20.0,
    }[seed]


def _training_terminals(campaign, programs):
    terminals = []
    for shard in assign_training_shards_v1(campaign):
        lanes = []
        for program in programs:
            for example in shard.examples:
                base = 1_000.0 + example.seed
                damage = base + (
                    _candidate_delta(example.seed)
                    if program.program_id == "fixture-sparse-candidate"
                    else 0.0
                )
                lanes.append(
                    {
                        "master_seed": example.seed,
                        "simulator_seed": 50_000 + example.seed,
                        "status": "COMPLETE",
                        "own_effective_damage": damage,
                        "own_effective_dps": damage / 10.0,
                        "ttk_ms": 10_000,
                        "error": None,
                        "program_ref": program.program_id,
                        "first_wave_arrival_ms": example.first_wave_arrival_ms,
                    }
                )
        terminals.append(
            (
                shard,
                {
                    "schema": TRAIN_TERMINAL_SCHEMA,
                    "terminal_status": "COMPLETE",
                    "campaign_id": campaign.campaign_id,
                    "build_id": campaign.build_id,
                    "campaign_contract": _campaign_contract_v1(campaign),
                    "loadout_id": shard.loadout_id,
                    "seed_shard_index": shard.seed_shard_index,
                    "examples": [row.to_dict() for row in shard.examples],
                    "programs": [_receipt(row) for row in programs],
                    "lanes": lanes,
                },
            )
        )
    return terminals


def _heldout_summary(campaign) -> dict:
    baseline = "shared_burst::cat.fury.profile1"
    strata = []
    for arrival in (0, 3_000):
        strata.append(
            {
                "first_wave_arrival_ms": arrival,
                "seed_count": 4,
                "policy_means": {
                    "frozen_candidate": {"mean_own_effective_damage": 1_010.0},
                    baseline: {"mean_own_effective_damage": 1_000.0},
                },
                "paired_candidate_minus_baseline_damage_statistics": {
                    baseline: {
                        "pair_count": 4,
                        "mean_damage_delta": 10.0,
                        "normal_confidence_interval": {
                            "lower_bound": 9.0,
                            "upper_bound": 11.0,
                        },
                        "win_tie_loss": {"wins": 4, "ties": 0, "losses": 0},
                    }
                },
            }
        )
    return {
        "schema": SUMMARY_TERMINAL_SCHEMA,
        "terminal_status": "COMPLETE",
        "campaign_id": campaign.campaign_id,
        "build_id": campaign.build_id,
        "campaign_contract": _campaign_contract_v1(campaign),
        "selected_loadout_id": LOADOUT,
        "frozen_program_id": "fixture-sparse-candidate",
        "baseline_policy_ids": [baseline],
        "metric": {"by_first_wave_arrival_ms": strata},
    }


class FirstWaveStratifiedCandidateDiagnosticsV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.programs = _programs()
        self.campaign = _campaign(self.programs[0])
        self.terminals = _training_terminals(self.campaign, self.programs)
        self.heldout = _heldout_summary(self.campaign)

    def _reduce(self, **overrides):
        arguments = {
            "campaign": self.campaign,
            "shard_terminals": self.terminals,
            "heldout_summary": self.heldout,
            "expected_searched_program_count": 2,
            "minimum_validation_pairs_per_stratum": 2,
        }
        arguments.update(overrides)
        return reduce_first_wave_stratified_candidate_diagnostics_v1(**arguments)

    def test_reports_every_candidate_and_gates_each_stratum_independently(self) -> None:
        result = self._reduce()
        self.assertEqual(SCHEMA, result["schema"])
        self.assertEqual({LOADOUT: 2}, result["searched_program_count_per_loadout"])
        self.assertEqual(2, len(result["candidate_registry"]))
        self.assertNotIn("program_key", result["candidate_registry"][0])
        by_arrival = {
            row["first_wave_arrival_ms"]: row for row in result["strata"]
        }

        immediate = by_arrival[0]
        self.assertEqual(
            "fixture-sparse-candidate",
            immediate["proposal_champion"]["program_ref"],
        )
        self.assertEqual(
            "NONZERO_STRATUM_CHAMPION_ACCEPTED",
            immediate["selection_validation_gate"]["status"],
        )
        candidate = next(
            row
            for row in immediate["candidates_by_proposal_rank"]
            if row["program_ref"] == "fixture-sparse-candidate"
        )
        self.assertEqual(10.0, candidate["proposal"]["mean_paired_damage_delta"])
        self.assertEqual(
            {"wins": 2, "ties": 0, "losses": 0},
            candidate["selection_validation"]["win_tie_loss"],
        )
        self.assertGreater(
            candidate["selection_validation"]["normal_confidence_interval"][
                "lower_bound"
            ],
            0.0,
        )

        delayed = by_arrival[3_000]
        self.assertEqual(
            "FALLBACK_PAIRED_ZERO_LCB_NOT_POSITIVE",
            delayed["selection_validation_gate"]["status"],
        )
        delayed_candidate = next(
            row
            for row in delayed["candidates_by_proposal_rank"]
            if row["program_ref"] == "fixture-sparse-candidate"
        )
        self.assertEqual(
            {"wins": 1, "ties": 0, "losses": 1},
            delayed_candidate["selection_validation"]["win_tie_loss"],
        )
        self.assertLess(
            delayed_candidate["selection_validation"][
                "normal_confidence_interval"
            ]["lower_bound"],
            0.0,
        )
        self.assertEqual(1, result["gate_summary"]["accepted_nonzero_stratum_count"])

    def test_arrival_is_explicitly_diagnostic_not_a_policy_route(self) -> None:
        result = self._reduce()
        observability = result["policy_observability"]
        self.assertFalse(observability["directly_observable_by_policy"])
        self.assertFalse(observability["direct_arrival_stratum_routing_authorized"])
        self.assertIn("PREFIX_OBSERVABLE", observability["permitted_use"])
        self.assertTrue(
            result["contract"]["diagnostic_strata_are_not_deployable_policy_routes"]
        )
        self.assertIn(
            "DOES_NOT_VALIDATE_UNREPLAYED_STRATUM_CHAMPIONS",
            result["heldout_context"]["scope"],
        )

    def test_missing_searched_lane_is_rejected_instead_of_scored_as_zero(self) -> None:
        broken = deepcopy(self.terminals)
        broken[0][1]["lanes"] = broken[0][1]["lanes"][:-1]
        with self.assertRaisesRegex(ValueError, "coverage mismatch"):
            self._reduce(shard_terminals=broken)

    def test_mismatched_heldout_summary_is_rejected(self) -> None:
        heldout = deepcopy(self.heldout)
        heldout["campaign_id"] = "another-campaign"
        with self.assertRaisesRegex(ValueError, "matching campaign"):
            self._reduce(heldout_summary=heldout)

    def test_terminal_campaign_contract_is_required(self) -> None:
        broken = deepcopy(self.terminals)
        broken[0][1].pop("campaign_contract")
        with self.assertRaisesRegex(ValueError, "complete matching shard"):
            self._reduce(shard_terminals=broken)

    def test_searched_program_key_is_recomputed_from_program(self) -> None:
        broken = deepcopy(self.terminals)
        for _, terminal in broken:
            terminal["programs"][0]["program_key"] = "not-the-program-key"
        with self.assertRaisesRegex(ValueError, "receipt identity mismatch"):
            self._reduce(shard_terminals=broken)

    def test_frozen_searched_program_count_is_enforced(self) -> None:
        with self.assertRaisesRegex(ValueError, "searched program count"):
            self._reduce(expected_searched_program_count=309)

    def test_artifact_entrypoint_reads_each_named_training_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign_path = root / "campaign.json"
            training_root = root / "train"
            heldout_path = root / "summary.json"
            training_root.mkdir()
            campaign_path.write_text(
                json.dumps(self.campaign.to_dict()), encoding="utf-8"
            )
            heldout_path.write_text(json.dumps(self.heldout), encoding="utf-8")
            for shard, terminal in self.terminals:
                (training_root / train_terminal_name_v1(shard)).write_text(
                    json.dumps(terminal), encoding="utf-8"
                )

            result = reduce_first_wave_stratified_candidate_artifacts_v1(
                campaign_path=campaign_path,
                training_root=training_root,
                heldout_summary_path=heldout_path,
                expected_searched_program_count=2,
            )

        self.assertEqual("COMPLETE", result["terminal_status"])
        self.assertEqual(2, len(result["strata"]))


if __name__ == "__main__":
    unittest.main()
