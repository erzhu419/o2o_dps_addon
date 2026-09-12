from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from o2o_dps.fury_multiseed_hpc_dispatch_v3 import (
    ALLOWED_POLICY_IDS_V3,
    build_single_node_fixture_dispatch_v3,
)
from o2o_dps.fury_multiseed_hpc_reducer_v4 import (
    REDUCTION_SCHEMA_V4,
    FuryMultiseedHpcReducerV4Error,
    reduce_dispatch_v4,
)
from o2o_dps.fury_paired_multiseed_runner_v4 import (
    CAT2NEW_POLICY_ID,
    CAT_POLICY_ID,
    CONTRA260817_POLICY_ID,
    CONTRA_DEPLOYED_POLICY_ID,
    HISTORICAL_POLICY_ID,
)
from tests.test_fury_multiseed_hpc_four_lane_v3 import _plan


def _stats(*, eligible: bool = True) -> dict[str, int | float]:
    return {
        "rollout_count": 1,
        "damage_sum": 100.0,
        "elapsed_ms_sum": 1000,
        "dps_sum": 100.0,
        "dps_squared_sum": 10000.0,
        "completion_count": 1,
        "offline_score_eligible_count": int(eligible),
        "omitted_lane_count_sum": 0,
        "fatal_error_count_sum": 0,
    }


def _validated_rows() -> list[dict[str, object]]:
    dps = {
        CAT_POLICY_ID: 100.0,
        CONTRA_DEPLOYED_POLICY_ID: 102.0,
        CONTRA260817_POLICY_ID: 101.0,
        CAT2NEW_POLICY_ID: 105.0,
    }
    return [
        {
            "policy_identity": {"policy_id": policy_id},
            "scenario_identity": {
                "instance_id": "instance",
                "scenario_id": "scenario",
            },
            "sufficient_statistics": _stats(),
            "lane_result": {
                "completion_mode": "SCENARIO_HORIZON_REACHED",
                "elapsed_ms": 1000,
                "dps": dps[policy_id],
                "offline_score_eligible": True,
                "dynamic_runtime_receipts_complete": True,
            },
        }
        for policy_id in ALLOWED_POLICY_IDS_V3
    ]


class FuryMultiseedHpcReducerV4Tests(unittest.TestCase):
    def test_single_pass_returns_aggregates_and_two_traces(self) -> None:
        plan = _plan((17,))
        dispatch = build_single_node_fixture_dispatch_v3(plan)
        group_id = plan["contract"]["groups"][0]["group_id"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "receipts").mkdir()
            (root / "groups").mkdir()
            (root / "receipts" / f"{group_id}.json").write_text(
                json.dumps({"result_file": f"groups/{group_id}.jsonl"}),
                encoding="utf-8",
            )
            (root / "groups" / f"{group_id}.jsonl").write_text(
                "{}\n{}\n{}\n{}\n", encoding="utf-8"
            )
            with patch(
                "o2o_dps.fury_multiseed_hpc_reducer_v4._validate_group_receipt",
                return_value=_validated_rows(),
            ) as validator:
                result = reduce_dispatch_v4(
                    plan, dispatch, output_directory=root
                )

        self.assertEqual(1, validator.call_count)
        self.assertEqual(REDUCTION_SCHEMA_V4, result["schema"])
        self.assertEqual(4, result["unique_rollout_count"])
        self.assertEqual(
            ["candidate_minus_cat", "candidate_minus_contra260817"],
            sorted(result["paired_traces"]),
        )
        self.assertEqual(
            [CAT_POLICY_ID, CONTRA260817_POLICY_ID],
            result["trace_baseline_policy_ids"],
        )
        self.assertEqual(
            5.0,
            result["paired_traces"]["candidate_minus_cat"][0][
                "candidate_minus_baseline_dps"
            ],
        )
        self.assertEqual(
            4.0,
            result["paired_traces"]["candidate_minus_contra260817"][0][
                "candidate_minus_baseline_dps"
            ],
        )
        self.assertTrue(
            result["contrast_readiness"]["candidate_minus_cat"][
                "analysis_eligible"
            ]
        )
        self.assertEqual(
            [HISTORICAL_POLICY_ID],
            result["formal_missing_policy_ids"],
        )
        self.assertNotIn(
            "CONTRA260817_DYNAMIC_V3_FULL_POLICY_EXECUTOR_MISSING",
            result["formal_blocker_codes"],
        )

    def test_missing_receipt_is_an_integrity_error(self) -> None:
        plan = _plan((17,))
        dispatch = build_single_node_fixture_dispatch_v3(plan)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                FuryMultiseedHpcReducerV4Error, "worker receipt missing"
            ):
                reduce_dispatch_v4(plan, dispatch, output_directory=directory)


if __name__ == "__main__":
    unittest.main()
