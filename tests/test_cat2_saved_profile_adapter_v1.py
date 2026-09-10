from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.cat2_saved_profile_adapter_v1 import (
    Cat2SavedProfileAdapterError,
    Cat2SavedProfileSourceAdapterV1,
)
from o2o_dps.cat2_saved_profile_v1 import (
    ARTIFACT_TYPE,
    AUTHORITY_STATE,
    EXPECTED_CARD_ORDER,
    PINNED_BRAINOF_CAT_SOURCE_SHA256,
    PINNED_SOURCE_SHA256,
)
from o2o_dps.expert_policy import (
    WAIT_ACTION,
    ExpertRole,
    ProvenanceKind,
    StanceOp,
    SwingQueueOp,
)
from o2o_dps.fury_expert_adapters import (
    BLOODRAGE,
    BLOODTHIRST,
    EXECUTE,
    WHIRLWIND,
    FuryExpertState,
)


def _canonical_hash(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _snapshot() -> dict[str, object]:
    options = {
        "warrior_o2o_policy_brain": {"liveMode": False},
        "warrior_bloodrage": {"maximumRage": 30},
        "warrior_heroic_strike_alt": {"rageThreshold": 50},
    }
    profile = {
        "id": 1,
        "name": "BrainOfCat Shadow",
        "steps": [
            {
                "position": position,
                "id": card_id,
                "enabled": 1,
                "option_values": options.get(card_id, {}),
            }
            for position, card_id in enumerate(EXPECTED_CARD_ORDER, start=1)
        ],
    }
    semantic = {
        "cat2_schema_version": 1,
        "repository_schema_version": 1,
        "profile": profile,
    }
    sources = [
        {
            "root_kind": "cat2",
            "relative_path": relative,
            "sha256": digest,
            "size_bytes": 1,
            "mtime_ns": 1,
            "mtime_utc": "1970-01-01T00:00:00.000000Z",
        }
        for relative, digest in sorted(PINNED_SOURCE_SHA256.items())
    ] + [
        {
            "root_kind": "brainofcat",
            "relative_path": relative,
            "sha256": digest,
            "size_bytes": 1,
            "mtime_ns": 1,
            "mtime_utc": "1970-01-01T00:00:00.000000Z",
        }
        for relative, digest in sorted(PINNED_BRAINOF_CAT_SOURCE_SHA256.items())
    ]
    identity = [
        {
            "root_kind": item["root_kind"],
            "relative_path": item["relative_path"],
            "sha256": item["sha256"],
        }
        for item in sources
    ]
    raw_hash = "a" * 64
    source_hash = _canonical_hash(identity)
    return {
        "artifact_type": ARTIFACT_TYPE,
        "schema_version": 1,
        "status": "ok",
        "authority_state": AUTHORITY_STATE,
        "execution_authorized": False,
        "deployment_allowed": False,
        "savedvariables": {
            "path": r"D:\WOW\WTF\SavedVariables\Cat2.lua",
            "top_level_assignment": "Cat2CharacterDB",
            "size_bytes": 1,
            "mtime_ns": 1,
            "mtime_utc": "1970-01-01T00:00:00.000000Z",
            "sha256": raw_hash,
        },
        "selection": {
            "requested_profile_name": profile["name"],
            "active_profile_id": profile["id"],
            "next_profile_id": 2,
            "profile_order": [1],
            "profile_count": 1,
        },
        "profile": profile,
        "profile_semantic_document": semantic,
        "source_bundle": {
            "installed_root": r"D:\WOW\Interface\AddOns\Cat2",
            "brainofcat_root": r"D:\WOW\Interface\AddOns\BrainOfCat",
            "roots": {
                "cat2": r"D:\WOW\Interface\AddOns\Cat2",
                "brainofcat": r"D:\WOW\Interface\AddOns\BrainOfCat",
            },
            "scope": "DIRECT_RUNTIME_DEPENDENCY_PINNED",
            "transitive_dependency_closure_claimed": False,
            "pin_status": "PINNED_EXACT",
            "file_count": len(sources),
            "files": sources,
            "sha256": source_hash,
        },
        "raw_savedvariables_sha256": raw_hash,
        "profile_semantic_sha256": _canonical_hash(semantic),
        "source_bundle_sha256": source_hash,
    }


class Cat2SavedProfileSourceAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = Cat2SavedProfileSourceAdapterV1(_snapshot())

    def test_is_deployed_source_derived_but_never_an_independent_vote(self) -> None:
        decision = self.adapter.propose(
            FuryExpertState(
                rage=0,
                target_health_pct=50,
                bloodrage_ready=False,
                bloodthirst_ready_in_s=5,
                whirlwind_ready_in_s=5,
            )
        )
        self.assertTrue(decision.valid)
        self.assertEqual(decision.provenance.role, ExpertRole.DEPLOYED)
        self.assertEqual(decision.provenance.kind, ProvenanceKind.SOURCE_DERIVED)
        self.assertFalse(decision.eligible_for_independent_vote)
        self.assertEqual(decision.gcd, WAIT_ACTION)
        self.assertEqual(
            [sink.channel for sink in decision.raw_sink_order], ["autoattack"]
        )
        self.assertEqual(
            decision.metadata["profile_step_order"], list(EXPECTED_CARD_ORDER)
        )
        self.assertEqual(
            decision.metadata["evaluated_step_order"], list(EXPECTED_CARD_ORDER)
        )
        self.assertEqual(decision.metadata["raw_savedvariables_sha256"], "a" * 64)
        self.assertIn("profile_semantic_sha256", decision.metadata)
        self.assertIn("source_bundle_sha256", decision.metadata)

    def test_stance_stops_before_bloodrage_and_gcd_cards(self) -> None:
        decision = self.adapter.propose(
            FuryExpertState(
                rage=100,
                target_health_pct=10,
                current_stance=StanceOp.BATTLE,
                bloodrage_ready=True,
            )
        )
        self.assertEqual(decision.stance, StanceOp.BERSERKER)
        self.assertEqual(decision.gcd, WAIT_ACTION)
        self.assertEqual(decision.swing_queue, SwingQueueOp.KEEP)
        self.assertEqual(
            [sink.channel for sink in decision.raw_sink_order],
            ["autoattack", "stance"],
        )
        self.assertEqual(
            decision.metadata["evaluated_step_order"],
            list(EXPECTED_CARD_ORDER[:3]),
        )
        self.assertEqual(
            decision.metadata["stopped_by_card"], "warrior_berserker_stance"
        )

    def test_bloodrage_coexists_with_execute_and_execute_stops(self) -> None:
        decision = self.adapter.propose(
            FuryExpertState(
                rage=20,
                target_health_pct=19.89,
                execute_cost=15,
                bloodrage_ready=True,
                bloodthirst_ready_in_s=0,
                whirlwind_ready_in_s=0,
            )
        )
        self.assertEqual(decision.off_gcd, (BLOODRAGE,))
        self.assertEqual(decision.gcd, EXECUTE)
        self.assertEqual(decision.swing_queue, SwingQueueOp.KEEP)
        self.assertEqual(
            [sink.channel for sink in decision.raw_sink_order],
            ["autoattack", "off_gcd", "gcd"],
        )
        self.assertEqual(decision.metadata["stopped_by_card"], "warrior_execute")

    def test_execute_health_boundary_is_strict_and_bt_has_priority(self) -> None:
        state = FuryExpertState(
            rage=30,
            target_health_pct=19.9,
            execute_cost=15,
            bloodthirst_ready_in_s=0,
            whirlwind_ready_in_s=0,
        )
        at_boundary = self.adapter.propose(state)
        under_boundary = self.adapter.propose(replace(state, target_health_pct=19.899))
        self.assertEqual(at_boundary.gcd, BLOODTHIRST)
        self.assertEqual(
            at_boundary.metadata["stopped_by_card"], "warrior_bloodthirst"
        )
        self.assertEqual(under_boundary.gcd, EXECUTE)

    def test_whirlwind_uses_strict_ready_offset_and_inclusive_range(self) -> None:
        base = FuryExpertState(
            rage=25,
            target_health_pct=50,
            bloodthirst_known=False,
            whirlwind_cost=25,
            target_distance_yards=7.0,
            whirlwind_ready_in_s=0.499999,
        )
        ready = self.adapter.propose(base)
        at_offset = self.adapter.propose(replace(base, whirlwind_ready_in_s=0.5))
        outside = self.adapter.propose(
            replace(base, target_distance_yards=7.000001)
        )
        self.assertEqual(ready.gcd, WHIRLWIND)
        self.assertEqual(at_offset.gcd, WAIT_ACTION)
        self.assertEqual(outside.gcd, WAIT_ACTION)
        self.assertEqual(ready.metadata["stopped_by_card"], "warrior_whirlwind")
        self.assertEqual(
            ready.metadata["source_api_noop_retry_contracts"],
            [
                {
                    "lane": "gcd",
                    "action": WHIRLWIND,
                    "operation": "Cat2.Cast",
                    "minimum_ready_in_ms_inclusive": 1,
                    "maximum_ready_in_ms_exclusive": 500,
                    "retry_wait_ms": 100,
                    "source_ref": "Cards/Warrior/Whirlwind.lua:35-62",
                    "reason": "SpellReadyOffset<0.5 failed-cast retry window",
                }
            ],
        )

    def test_heroic_alt_queue_lane_does_not_stop_and_switches_at_two_targets(self) -> None:
        base = FuryExpertState(
            rage=50,
            target_health_pct=50,
            bloodthirst_known=False,
            whirlwind_ready_in_s=1,
            nearby_enemies=1,
        )
        single = self.adapter.propose(base)
        multiple = self.adapter.propose(replace(base, nearby_enemies=2))
        self.assertEqual(single.swing_queue, SwingQueueOp.HEROIC_STRIKE)
        self.assertEqual(multiple.swing_queue, SwingQueueOp.CLEAVE)
        self.assertEqual(single.gcd, WAIT_ACTION)
        self.assertIsNone(single.metadata["stopped_by_card"])
        self.assertFalse(single.metadata["step_trace"][-1]["stopped"])
        self.assertEqual(
            [sink.channel for sink in single.raw_sink_order],
            ["autoattack", "swing_queue"],
        )

    def test_bloodrage_threshold_is_strict_and_can_coexist_with_queue(self) -> None:
        # Use a modified but internally rehashed loader-shaped snapshot to make
        # both the off-GCD threshold and queue threshold observable together.
        snapshot = _snapshot()
        profile = snapshot["profile"]
        profile["steps"][3]["option_values"]["maximumRage"] = 60
        semantic = {
            "cat2_schema_version": 1,
            "repository_schema_version": 1,
            "profile": profile,
        }
        snapshot["profile_semantic_document"] = semantic
        snapshot["profile_semantic_sha256"] = _canonical_hash(semantic)
        adapter = Cat2SavedProfileSourceAdapterV1(snapshot)
        base = FuryExpertState(
            rage=50,
            target_health_pct=50,
            bloodrage_ready=True,
            bloodthirst_known=False,
            whirlwind_ready_in_s=1,
            nearby_enemies=1,
        )
        below = adapter.propose(base)
        at = adapter.propose(replace(base, rage=60))
        self.assertEqual(below.off_gcd, (BLOODRAGE,))
        self.assertEqual(below.swing_queue, SwingQueueOp.HEROIC_STRIKE)
        self.assertEqual(at.off_gcd, ())

    def test_bloodrage_unitxp_default_range_is_inclusive_five_yards(self) -> None:
        base = FuryExpertState(
            rage=0,
            target_health_pct=50,
            in_melee_range=True,
            target_distance_yards=5.0,
            bloodrage_ready=True,
            bloodthirst_known=False,
            whirlwind_ready_in_s=1,
        )
        at_boundary = self.adapter.propose(base)
        outside = self.adapter.propose(
            replace(base, target_distance_yards=5.000001)
        )
        self.assertEqual(at_boundary.off_gcd, (BLOODRAGE,))
        self.assertEqual(outside.off_gcd, ())
        self.assertEqual(
            at_boundary.metadata["step_trace"][3]["effect"],
            "bloodrage_sink_continue",
        )
        self.assertEqual(
            outside.metadata["step_trace"][3]["effect"],
            "bloodrage_condition_false",
        )

    def test_malformed_or_tampered_snapshot_fails_closed(self) -> None:
        mutations = []
        wrong_authority = _snapshot()
        wrong_authority["authority_state"] = "SEALED"
        mutations.append(wrong_authority)
        live = _snapshot()
        live["profile"]["steps"][0]["option_values"]["liveMode"] = True
        mutations.append(live)
        reordered = _snapshot()
        reordered["profile"]["steps"][1], reordered["profile"]["steps"][2] = (
            reordered["profile"]["steps"][2],
            reordered["profile"]["steps"][1],
        )
        mutations.append(reordered)
        bad_semantic_hash = _snapshot()
        bad_semantic_hash["profile_semantic_sha256"] = "b" * 64
        mutations.append(bad_semantic_hash)
        bad_source = _snapshot()
        bad_source["source_bundle"]["files"][0]["sha256"] = "c" * 64
        mutations.append(bad_source)

        for index, snapshot in enumerate(mutations):
            with self.subTest(index=index), self.assertRaises(
                Cat2SavedProfileAdapterError
            ):
                Cat2SavedProfileSourceAdapterV1(snapshot)


if __name__ == "__main__":
    unittest.main()
