from __future__ import annotations

import unittest

from copy import deepcopy
import json
from pathlib import Path

from scripts.development_d900_v7_formal_cat_diagnostic_remote_v1 import (
    _all_white_basis_counts, _check_same_frozen_case, _early_spell_rows,
    _early_white_basis_rows, _white_comparison,
)


V56 = Path(__file__).resolve().parents[1] / "results/responsive-team-v4/v56-d900-v6-cat-target-diagnostic-seed1.json"


class V7FormalCatDiagnosticRemoteTests(unittest.TestCase):
    def test_applied_early_white_aggregates_and_keeps_logged_raw_separate(self) -> None:
        diagnostic = {"groups": [
            {"time_bucket": "through_9098ms", "event_type": "DMG", "target_index": 0,
             "spell_id": 6603, "target_selection_basis": "HEAD", "event_count": 2,
             "applied_hit_count": 2, "applied_damage": 100.0},
            {"time_bucket": "through_9098ms", "event_type": "DMG", "target_index": 0,
             "spell_id": 6603, "target_selection_basis": "FALLBACK", "event_count": 1,
             "applied_hit_count": 1, "applied_damage": 40.0},
            {"time_bucket": "after_9098ms", "event_type": "DMG", "target_index": 0,
             "spell_id": 6603, "target_selection_basis": "HEAD", "event_count": 1,
             "applied_hit_count": 1, "applied_damage": 200.0},
        ]}
        current = _early_spell_rows(diagnostic)
        self.assertEqual(3, current[0]["applied_hit_count"])
        self.assertEqual(140.0, current[0]["applied_damage"])
        self.assertEqual({"HEAD", "FALLBACK"}, {
            row["target_selection_basis"] for row in _early_white_basis_rows(diagnostic)
        })
        self.assertEqual([
            {"target_selection_basis": "FALLBACK", "event_count": 1,
             "applied_hit_count": 1, "applied_damage": 40.0},
            {"target_selection_basis": "HEAD", "event_count": 3,
             "applied_hit_count": 3, "applied_damage": 300.0},
        ], _all_white_basis_counts(diagnostic))
        prior = [{"target_index": 0, "spell_id": 6603, "applied_hit_count": 1, "applied_damage": 80.0}]
        historical = {"target_guids": ["F240", "F244", "F245"], "per_spell": [
            {"cutoff_ms_inclusive": 9098, "owner": "teammate_direct", "target_guid": "F240",
             "spell_id": 6603, "hit_count": 4, "raw_damage": 210},
        ]}
        comparison = _white_comparison(current, prior, historical)
        self.assertEqual((4, 210, 1, 80.0, 3, 140.0), (
            comparison[0]["historical_logged_raw_hits"], comparison[0]["historical_logged_raw_damage"],
            comparison[0]["v56_applied_hits"], comparison[0]["v56_applied_damage"],
            comparison[0]["v7_applied_hits"], comparison[0]["v7_applied_damage"],
        ))
        self.assertEqual(0, comparison[2]["v7_applied_hits"])

    def test_same_wave_comparison_rejects_a_different_source_binding(self) -> None:
        prior = json.loads(V56.read_text(encoding="utf-8"))
        _check_same_frozen_case(prior, prior)
        changed = deepcopy(prior)
        changed["frozen_source_binding"]["component_id"] = "different"
        with self.assertRaisesRegex(RuntimeError, "source binding differs"):
            _check_same_frozen_case(changed, prior)


if __name__ == "__main__":
    unittest.main()
