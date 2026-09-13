from __future__ import annotations

import unittest
from unittest.mock import patch

from o2o_dps import historical_fury_pull_origin_feasibility_v1 as feasibility


class PullOriginFeasibilityTests(unittest.TestCase):
    def test_chronicle_deltas_do_not_make_origin_exact(self) -> None:
        fields = {
            name: {"observation_category": "EXACT" if name == "player.stance" else "MISSING"}
            for name in feasibility.REQUIRED_FIELDS
        }
        source = {
            "checkpoint_contract": {"required_fields": list(feasibility.REQUIRED_FIELDS)},
            "content_address": {"sha256": "source-address"},
            "rows": [
                {
                    "source_identity": {"instance_id": "i", "wave_id": "w"},
                    "segment_ref": "segment",
                    "window_start": {"cutoff_exclusive_order_key": [100, 2, 0, 0]},
                    "exact_checkpoint_ready": False,
                    "fields": fields,
                }
            ],
        }
        with patch.object(feasibility, "validate_prefix_manifest", return_value=source):
            result = feasibility.compile_feasibility(source)
        row = result["rows"][0]
        self.assertFalse(row["strict_pull_replay_exact"])
        self.assertFalse(row["pull_origin_checkpoint_observed"])
        self.assertIn("player.rage_current", row["window_nonexact_fields"])
        self.assertNotIn("player.stance", row["window_nonexact_fields"])
        self.assertFalse(result["authorization"]["exact_source_comparison"])

    def test_changed_source_contract_is_rejected(self) -> None:
        source = {
            "checkpoint_contract": {"required_fields": ["player.stance"]},
            "rows": [],
        }
        with patch.object(feasibility, "validate_prefix_manifest", return_value=source):
            with self.assertRaisesRegex(ValueError, "contract"):
                feasibility.compile_feasibility(source)


if __name__ == "__main__":
    unittest.main()
