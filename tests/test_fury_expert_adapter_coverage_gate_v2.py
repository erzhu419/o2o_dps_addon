from __future__ import annotations

import copy
import gzip
import json
import unittest

from o2o_dps.fury_expert_adapter_coverage_gate_v2 import (
    ALLOWED_STATUSES,
    CAT2_POLICY,
    CAT_POLICY,
    EXECUTE,
    NOT_COVERED,
    SOURCE_DECLARED_NOOP,
    WHIRLWIND,
    _classify_cat2_noop,
    _classify_cat_omission,
    _contra_health_bucket,
    _ledger_bytes,
    _source_site_manifest,
    _target_health_evidence,
)


def _matrix(policy_id: str) -> dict[str, object]:
    return {
        "episode_key_sha256": "e" * 64,
        "policy_id": policy_id,
        "target_count_stratum": "1",
        "duration_stratum": "long_gt_30s",
        "loadout": "test_two_hand",
        "starting_armor": 4211,
    }


def _cat_step() -> dict[str, object]:
    return {
        "decision_index": 7,
        "proposal": {
            "valid": True,
            "gcd": {"action": EXECUTE, "wait_ms": None},
            "raw_sink_order": [
                {
                    "channel": "gcd",
                    "operation": "CastSpellByName",
                    "value": "斩杀",
                    "source_ref": "WarriorFury.lua:407-411",
                }
            ],
        },
        "expert_state": {
            "rage": 9.999,
            "execute_cost": 10.0,
            "target_is_boss": False,
            "target_is_training_dummy": False,
            "target_health_pct": 19.999,
            "gcd_ready": True,
            "weapon_mode": "TWO_HAND",
        },
        "simulator_state_before": {
            "time_ms": 1234,
            "execute_phase_20": True,
            "target_health_known": False,
            "current_cast": None,
        },
    }


def _cat_omission() -> dict[str, object]:
    return {
        "lane": "gcd",
        "requested": EXECUTE,
        "reason": "action_not_legal:ready_in_ms=0",
    }


def _cat2_step() -> dict[str, object]:
    return {
        "decision_index": 11,
        "proposal": {
            "valid": True,
            "gcd": {"action": WHIRLWIND, "wait_ms": None},
            "raw_sink_order": [
                {
                    "channel": "gcd",
                    "operation": "Cat2.Cast",
                    "value": "旋风斩",
                    "source_ref": "Cards/Warrior/Whirlwind.lua:35-62",
                }
            ],
        },
        "expert_state": {
            "weapon_mode": "TWO_HAND",
            "target_is_boss": False,
            "target_is_training_dummy": False,
        },
        "simulator_state_before": {"time_ms": 2000},
    }


def _cat2_command(ready_in_ms: int) -> dict[str, object]:
    return {
        "lane": "gcd",
        "operation": "source_api_noop_wait",
        "requested": WHIRLWIND,
        "status": "known_noop",
        "available": {
            "legal": False,
            "ready_in_ms": ready_in_ms,
            "triggers_gcd": True,
        },
        "result": {"wait_ms": 100},
    }


class FuryExpertAdapterCoverageGateV2Tests(unittest.TestCase):
    def test_cat_low_rage_execute_is_source_declared_noop_only_at_full_boundary(self) -> None:
        event = _classify_cat_omission(
            _matrix(CAT_POLICY), _cat_step(), _cat_omission(), event_ordinal=1
        )
        self.assertEqual(event["status"], SOURCE_DECLARED_NOOP)
        self.assertEqual(event["noop_blocker"], "RAGE_BELOW_EXECUTE_COST")
        self.assertEqual(event["retry_timing_authority"], "RUNNER_FIXED_100MS_PROXY")

        mutations = []
        rage_equal = _cat_step()
        rage_equal["expert_state"]["rage"] = 10.0
        mutations.append(rage_equal)
        boss = _cat_step()
        boss["expert_state"]["target_is_boss"] = True
        mutations.append(boss)
        health_boundary = _cat_step()
        health_boundary["expert_state"]["target_health_pct"] = 20.0
        mutations.append(health_boundary)
        casting = _cat_step()
        casting["simulator_state_before"]["current_cast"] = {
            "action": {"spell_id": 45961}
        }
        mutations.append(casting)
        for step in mutations:
            with self.subTest(step=step):
                classified = _classify_cat_omission(
                    _matrix(CAT_POLICY), step, _cat_omission(), event_ordinal=1
                )
                self.assertEqual(classified["status"], NOT_COVERED)

    def test_cat_non_rage_illegal_reason_is_not_hidden(self) -> None:
        omission = _cat_omission()
        omission["reason"] = "action_not_legal:ready_in_ms=100"
        event = _classify_cat_omission(
            _matrix(CAT_POLICY), _cat_step(), omission, event_ordinal=1
        )
        self.assertEqual(event["status"], NOT_COVERED)

    def test_cat2_cooldown_window_boundaries_fail_closed(self) -> None:
        for ready in (1, 100, 400, 499):
            with self.subTest(ready=ready):
                event = _classify_cat2_noop(
                    _matrix(CAT2_POLICY),
                    _cat2_step(),
                    _cat2_command(ready),
                    event_ordinal=1,
                )
                self.assertEqual(event["status"], SOURCE_DECLARED_NOOP)
        for ready in (0, 500):
            with self.subTest(ready=ready):
                event = _classify_cat2_noop(
                    _matrix(CAT2_POLICY),
                    _cat2_step(),
                    _cat2_command(ready),
                    event_ordinal=1,
                )
                self.assertEqual(event["status"], NOT_COVERED)

    def test_contra_max_health_buckets_have_exact_boundaries(self) -> None:
        expected = {
            None: None,
            24999: "lt_25000",
            25000: "25000_to_lt_51000",
            50999: "25000_to_lt_51000",
            51000: "gte_51000",
        }
        for value, bucket in expected.items():
            with self.subTest(value=value):
                self.assertEqual(_contra_health_bucket(value), bucket)

    def test_missing_health_request_is_explicit(self) -> None:
        request = {
            "encounter": {
                "useHealth": False,
                "targets": [
                    {
                        "name": "历史目标",
                        "level": 60,
                        "mobType": "MobTypeUnknown",
                        "stats": [0] * 46,
                    }
                ],
            }
        }
        evidence = _target_health_evidence(request)
        self.assertFalse(evidence["target_max_health_available"])
        self.assertFalse(evidence["true_health_percent_available"])
        self.assertEqual(evidence["target_health_stat"], 0)

        known = copy.deepcopy(request)
        known["encounter"]["useHealth"] = True
        known["encounter"]["targets"][0]["stats"][34] = 51000
        evidence = _target_health_evidence(known)
        self.assertTrue(evidence["target_max_health_available"])
        self.assertTrue(evidence["true_health_percent_available"])

    def test_source_manifest_has_exact_four_state_taxonomy(self) -> None:
        manifest = _source_site_manifest()
        self.assertEqual(
            set(manifest["classification_taxonomy"]), set(ALLOWED_STATUSES)
        )
        self.assertFalse(manifest["nonclaims"]["all_source_sites_enumerated"])
        self.assertFalse(manifest["nonclaims"]["exact_lua_execution"])

    def test_not_covered_does_not_discard_contra_as_baseline(self) -> None:
        contract = _source_site_manifest()
        contra = next(
            site
            for site in contract["sites"]
            if site["source_branch_id"]
            == "contra_twohand_raid_a_nonboss_health_bucket"
        )
        self.assertEqual(
            contra["classification_when_any_required_field_missing"], NOT_COVERED
        )
        self.assertNotEqual(NOT_COVERED, "REJECTED_BASELINE")

    def test_ledger_gzip_is_deterministic_and_round_trips(self) -> None:
        rows = [
            {"event_id": "a", "status": SOURCE_DECLARED_NOOP},
            {"event_id": "b", "status": NOT_COVERED},
        ]
        first, first_payload = _ledger_bytes(rows)
        second, second_payload = _ledger_bytes(rows)
        self.assertEqual(first, second)
        self.assertEqual(first_payload, second_payload)
        decoded = [
            json.loads(line)
            for line in gzip.decompress(first).decode("utf-8").splitlines()
        ]
        self.assertEqual(decoded, rows)


if __name__ == "__main__":
    unittest.main()
