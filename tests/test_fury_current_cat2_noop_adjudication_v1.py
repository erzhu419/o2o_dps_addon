from __future__ import annotations

from collections import Counter
from pathlib import Path
import tempfile
import unittest

from o2o_dps.fury_current_cat2_noop_adjudication_v1 import (
    EXPECTED_CAT2_ROLLOUT_COUNT,
    EXPECTED_PROXY_EVENT_COUNT,
    MECHANISM_CLASSIFICATION,
    NOMINAL_RETRY_WAIT_MS,
    NoopAdjudicationError,
    NoopEvidenceAccumulator,
    OLD_REASON,
    TIMING_PROXY_REASON,
    adjudicate_step,
    build_adjudication_artifact,
    capture_sidecar_sources,
    expected_full_proxy_count,
    write_adjudication_with_receipt,
)
from o2o_dps.fury_current_cat2_short_horizon_sensitivity_v1 import (
    verify_upstream_evidence,
)


def _step(*, ready_ms: int = 400, gcd_ms: int = 0, wait_ms: int = 100) -> dict:
    return {
        "commands": [
            {
                "lane": "gcd",
                "operation": "source_api_noop_wait",
                "requested": "warrior.whirlwind",
                "action": {"spell_id": 1680},
                "source_attempts": [
                    {
                        "channel": "gcd",
                        "operation": "Cat2.Cast",
                        "value": "Whirlwind",
                        "source_ref": "Cards/Warrior/Whirlwind.lua:35-62",
                    }
                ],
                "available": {
                    "label": "SpellID: 1680",
                    "legal": False,
                    "ready_in_ms": ready_ms,
                    "triggers_gcd": True,
                },
                "status": "known_noop",
                "result": {"wait_ms": wait_ms},
            }
        ],
        "nonfaithful_reasons": [OLD_REASON],
        "simulator_state_before": {
            "power": {"type": "rage", "current": 37.9, "maximum": 100},
            "gcd_remaining_ms": gcd_ms,
            "current_cast": None,
            "needs_input": True,
            "finished": False,
        },
        "expert_state": {
            "rage": 37.0,
            "whirlwind_cost": 25.0,
            "current_stance": "BERSERKER",
            "target_exists": True,
            "target_distance_yards": 5.0,
        },
        "proposal": {
            "gcd": {"action": "warrior.whirlwind", "wait_ms": None},
            "metadata": {
                "source_api_noop_retry_contracts": [
                    {
                        "lane": "gcd",
                        "action": "warrior.whirlwind",
                        "operation": "Cat2.Cast",
                        "minimum_ready_in_ms_inclusive": 1,
                        "maximum_ready_in_ms_exclusive": 500,
                        "retry_wait_ms": NOMINAL_RETRY_WAIT_MS,
                    }
                ]
            },
        },
    }


def _formal_accumulator() -> NoopEvidenceAccumulator:
    value = NoopEvidenceAccumulator(
        event_count=EXPECTED_PROXY_EVENT_COUNT,
        rollout_count=EXPECTED_CAT2_ROLLOUT_COUNT,
        rollouts_with_event=3344,
    )
    value.ready_in_ms.update({100: 4693, 200: 4797, 300: 4937, 400: 5002})
    value.gcd_remaining_ms.update({0: EXPECTED_PROXY_EVENT_COUNT})
    value.commanded_wait_ms.update({100: EXPECTED_PROXY_EVENT_COUNT})
    value.events_by_target_stratum.update(
        {"1": 2982, "2": 990, "3-4": 8039, "5+": 7418}
    )
    value.events_per_rollout.update(
        {0: EXPECTED_CAT2_ROLLOUT_COUNT - 3344, 4: 3344}
    )
    value.evidence_true_counts.update(
        {"spell_cooldown_positive_below_500ms": EXPECTED_PROXY_EVENT_COUNT}
    )
    return value


class CurrentCat2NoopAdjudicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.evidence = verify_upstream_evidence()

    def test_pinned_full_contains_exact_old_reason_count(self) -> None:
        self.assertEqual(
            expected_full_proxy_count(self.evidence),
            EXPECTED_PROXY_EVENT_COUNT,
        )

    def test_proxy_count_must_be_explicitly_pinned(self) -> None:
        with self.assertRaisesRegex(NoopAdjudicationError, "proxy count changed"):
            expected_full_proxy_count(
                self.evidence,
                expected_proxy_count=EXPECTED_PROXY_EVENT_COUNT + 1,
            )

    def test_one_event_has_cooldown_sufficient_evidence(self) -> None:
        accumulator = NoopEvidenceAccumulator()
        count = adjudicate_step(
            _step(), target_count_stratum="3-4", accumulator=accumulator
        )
        self.assertEqual(count, 1)
        self.assertEqual(accumulator.event_count, 1)
        self.assertEqual(accumulator.ready_in_ms, Counter({400: 1}))
        self.assertEqual(accumulator.gcd_remaining_ms, Counter({0: 1}))
        self.assertEqual(accumulator.commanded_wait_ms, Counter({100: 1}))
        self.assertEqual(sum(accumulator.violations.values()), 0)
        self.assertEqual(
            accumulator.evidence_true_counts[
                "spell_cooldown_positive_below_500ms"
            ],
            1,
        )

    def test_horizon_capped_wait_does_not_change_cooldown_classification(self) -> None:
        accumulator = NoopEvidenceAccumulator()
        adjudicate_step(
            _step(wait_ms=37),
            target_count_stratum="1",
            accumulator=accumulator,
        )
        self.assertEqual(sum(accumulator.violations.values()), 0)
        self.assertEqual(accumulator.commanded_wait_ms, Counter({37: 1}))

    def test_nonpositive_cooldown_is_a_violation(self) -> None:
        accumulator = NoopEvidenceAccumulator()
        adjudicate_step(
            _step(ready_ms=0),
            target_count_stratum="1",
            accumulator=accumulator,
        )
        self.assertEqual(
            accumulator.violations["spell_cooldown_positive_below_500ms"], 1
        )

    def test_sidecar_sources_are_content_addressed(self) -> None:
        identities = capture_sidecar_sources()
        self.assertEqual(len(identities), 7)
        self.assertEqual(len({value.path for value in identities}), 7)
        self.assertTrue(all(len(value.sha256) == 64 for value in identities))

    def test_artifact_reclassifies_mechanism_but_preserves_timing_proxy(self) -> None:
        artifact = build_adjudication_artifact(
            evidence=self.evidence,
            accumulator=_formal_accumulator(),
            validation_seeds=tuple(range(16)),
            family_count=392,
            sidecar_sources=capture_sidecar_sources(),
        )
        adjudication = artifact["adjudication"]
        self.assertEqual(
            adjudication["mechanism_classification"], MECHANISM_CLASSIFICATION
        )
        self.assertEqual(adjudication["unresolved_unknown_blocker_count"], 0)
        self.assertEqual(
            adjudication["remaining_nonfaithful_reason_counts"],
            {TIMING_PROXY_REASON: EXPECTED_PROXY_EVENT_COUNT},
        )
        self.assertFalse(
            artifact["bridge_field_inventory"][
                "bridge_change_required_for_this_whirlwind_adjudication"
            ]
        )
        self.assertTrue(artifact["claim_boundary"]["simulator_diagnostic_only"])
        for key, value in artifact["claim_boundary"].items():
            if key != "simulator_diagnostic_only":
                self.assertFalse(value, key)

    def test_artifact_fails_closed_on_any_violation(self) -> None:
        accumulator = _formal_accumulator()
        accumulator.violations["test"] = 1
        with self.assertRaisesRegex(NoopAdjudicationError, "violations"):
            build_adjudication_artifact(
                evidence=self.evidence,
                accumulator=accumulator,
                validation_seeds=tuple(range(16)),
                family_count=392,
                sidecar_sources=capture_sidecar_sources(),
            )

    def test_receipt_keeps_all_authorization_fields_false(self) -> None:
        artifact = build_adjudication_artifact(
            evidence=self.evidence,
            accumulator=_formal_accumulator(),
            validation_seeds=tuple(range(16)),
            family_count=392,
            sidecar_sources=capture_sidecar_sources(),
        )
        with tempfile.TemporaryDirectory() as temporary:
            receipt = write_adjudication_with_receipt(
                Path(temporary) / "result.json",
                artifact,
                evidence=self.evidence,
            )
        self.assertFalse(receipt["voting_result"])
        self.assertFalse(receipt["deployment_allowed"])
        self.assertFalse(receipt["real_game_superiority_claimed"])


if __name__ == "__main__":
    unittest.main()
