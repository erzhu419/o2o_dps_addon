from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.beam_search import FactorizedDecision
from o2o_dps.expert_policy import StanceOp
from o2o_dps.fury_expert_adapters import CatFurySourceAdapter, WeaponMode
from o2o_dps.fury_expert_guided_search_v1 import (
    DEFAULT_CAT2_PROFILE_SNAPSHOT,
    UNAVAILABLE_COOLDOWN_S,
    _add_exploration_candidates,
    _add_candidate,
    _factorized_candidate_id,
    _write_candidate_evaluation_jsonl,
    build_expert_guided_artifact,
    fury_state_from_simulator,
)
from o2o_dps.sim_bridge import ActionRef, AvailableAction


def _available(
    spell_id: int, *, tag: int = 0, gcd: bool, legal: bool = True
) -> AvailableAction:
    action = ActionRef(spell_id=spell_id, tag=tag)
    return AvailableAction(0, action, str(action), legal, 0, gcd)


class _RootOnlyBridge:
    def load(self, request: object, seed: int) -> dict[str, object]:
        return {
            "time_ms": 0,
            "damage_done": 0,
            "power": {"current": 100},
            "gcd_remaining_ms": 0,
            "mh_swing_remaining_ms": 1000,
            "mh_swing_duration_ms": 2400,
            "oh_swing_remaining_ms": 800,
            "execute_phase_20": False,
            "auras": [],
        }

    def actions(self) -> list[AvailableAction]:
        return [
            _available(23894, gcd=True),
            _available(1680, gcd=True),
            _available(45961, gcd=True),
            _available(20662, gcd=True),
            _available(2687, gcd=False),
            _available(25286, tag=1, gcd=False),
            _available(20569, tag=1, gcd=False),
        ]


def _benchmark_stub(
    bridge: object,
    request: object,
    *,
    seeds: object,
    candidates: object,
    horizon_ms: int,
    reference_candidate_id: str,
) -> dict[str, object]:
    candidate_rows = [candidate.to_dict() for candidate in candidates]
    return {
        "schema_version": 1,
        "kind": "fury_fixed_horizon_prefix_benchmark",
        "horizon_ms": horizon_ms,
        "seeds": list(seeds),
        "reference_candidate_id": reference_candidate_id,
        "candidate_count": len(candidate_rows),
        "trial_count": 0,
        "ranking": [],
        "trials": [],
    }


class FuryExpertGuidedSearchTests(unittest.TestCase):
    @staticmethod
    def _request() -> dict[str, object]:
        return json.loads(
            (PROJECT_ROOT / "configs" / "wowsims" / "fury_warrior_clean_dual.json")
            .read_text(encoding="utf-8")
        )

    def test_bridge_root_projects_to_dual_wield_source_state(self) -> None:
        request = json.loads(
            (PROJECT_ROOT / "configs" / "wowsims" / "fury_warrior_clean_dual.json")
            .read_text(encoding="utf-8")
        )
        state = {
            "power": {"current": 100},
            "gcd_remaining_ms": 0,
            "mh_swing_remaining_ms": 153,
            "mh_swing_duration_ms": 2379,
            "oh_swing_remaining_ms": 1617,
            "execute_phase_20": False,
            "auras": [
                {
                    "label": "Berserker Stance",
                    "stacks": 0,
                    "remaining_ms": -1,
                }
            ],
        }
        actions = [
            _available(23894, gcd=True),
            _available(1680, gcd=True),
            _available(2687, gcd=False),
            _available(12328, gcd=True),
        ]
        projected = fury_state_from_simulator(state, actions, request)

        self.assertEqual(projected.weapon_mode, WeaponMode.DUAL_WIELD)
        self.assertEqual(projected.current_stance, StanceOp.BERSERKER)
        self.assertAlmostEqual(projected.contra_st_s, 0.153)
        self.assertAlmostEqual(projected.contra_ss_s, 2.226)
        self.assertAlmostEqual(projected.contra_sd_s, 2.379)
        self.assertFalse(projected.has_battle_shout)
        self.assertFalse(projected.flurry_active)
        self.assertEqual(projected.target_health_pct, 100.0)

    def test_bridge_fractional_rage_uses_game_unitmana_floor(self) -> None:
        request = json.loads(
            (PROJECT_ROOT / "configs" / "wowsims" / "fury_warrior_clean_dual.json")
            .read_text(encoding="utf-8")
        )
        state = {
            "power": {"current": 29.9},
            "gcd_remaining_ms": 0,
            "mh_swing_remaining_ms": 1000,
            "mh_swing_duration_ms": 2400,
            "oh_swing_remaining_ms": 800,
            "execute_phase_20": False,
            "auras": [],
        }
        actions = [
            _available(23894, gcd=True),
            _available(1680, gcd=True),
        ]

        projected = fury_state_from_simulator(state, actions, request)

        self.assertEqual(projected.rage, 29.0)

    def test_unlearned_bloodthirst_remains_unavailable_in_strict_native_trace(self) -> None:
        state = _RootOnlyBridge().load({}, 1)
        state["oh_swing_remaining_ms"] = None  # two-hand historical Warrior
        actions = [
            _available(1680, gcd=True),
            _available(25286, tag=1, gcd=False),
        ]
        projected = fury_state_from_simulator(state, actions, self._request())
        self.assertEqual(projected.weapon_mode, WeaponMode.TWO_HAND)
        self.assertFalse(projected.bloodthirst_known)
        self.assertEqual(projected.bloodthirst_ready_in_s, UNAVAILABLE_COOLDOWN_S)
        self.assertGreater(projected.bloodthirst_ready_in_s, 60 * 60)
        json.dumps(asdict(projected), allow_nan=False)
        proposal = CatFurySourceAdapter().propose(projected)
        self.assertNotEqual(proposal.gcd, "warrior.bloodthirst")

    def test_exploration_is_factorized_and_includes_queue_plus_wait(self) -> None:
        sources = {}
        decisions = {}
        legal = {
            ActionRef(spell_id=23894),
            ActionRef(spell_id=25286, tag=1),
            ActionRef(spell_id=20569, tag=1),
        }
        _add_exploration_candidates(sources, decisions, legal)
        ids = {_factorized_candidate_id(decision) for decision in decisions.values()}

        self.assertIn("queue_keep__gcd_bloodthirst", ids)
        self.assertIn("queue_heroic_strike__gcd_bloodthirst", ids)
        self.assertIn("queue_cleave__gcd_wait_100ms", ids)
        self.assertTrue(all(value == {"explore"} for value in sources.values()))

    def test_candidate_provenance_retains_the_exact_proposal_seeds(self) -> None:
        sources = {}
        source_seeds = {}
        decisions = {}
        decision = FactorizedDecision(gcd=ActionRef(23894))

        _add_candidate(
            sources,
            decisions,
            decision,
            "cat",
            source_seeds=source_seeds,
            seed=11,
        )
        _add_candidate(
            sources,
            decisions,
            decision,
            "cat",
            source_seeds=source_seeds,
            seed=13,
        )

        key = next(iter(decisions))
        self.assertEqual(source_seeds[key]["cat"], {11, 13})

    def test_jsonl_is_explicitly_candidate_evaluation_not_teacher_data(self) -> None:
        artifact = {
            "generated_at": "2026-09-01T00:00:00Z",
            "validation_status": "SIMULATOR_FIXED_HORIZON_PREFIX_BENCHMARK_ONLY",
            "candidate_union_scope": {
                "kind": "GLOBAL_UNION_ACROSS_ROOT_SEEDS",
                "per_state_policy_label": False,
            },
            "cat2_profile_usage": {
                "proposal_row_count": 0,
                "distinct_candidate_count": 0,
                "exclusive_final_candidate_count": 0,
                "search_guidance_effect": "NOT_AVAILABLE_NO_PROFILE",
            },
            "benchmark": {"ranking": [], "trials": []},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidate-evaluation.jsonl"
            _write_candidate_evaluation_jsonl(path, artifact)
            header = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(
            header["kind"], "fury_prefix_candidate_evaluation_header"
        )
        self.assertFalse(header["training_eligible"])
        self.assertFalse(header["contains_state_action_next_state_tuples"])
        self.assertEqual(
            header["cat2_profile_usage"]["search_guidance_effect"],
            "NOT_AVAILABLE_NO_PROFILE",
        )

    @patch(
        "o2o_dps.fury_expert_guided_search_v1.benchmark_prefix_candidates",
        side_effect=_benchmark_stub,
    )
    def test_committed_cat2_snapshot_is_source_derived_deployed_and_non_voting(
        self, benchmark: object
    ) -> None:
        payload = DEFAULT_CAT2_PROFILE_SNAPSHOT.read_bytes()
        snapshot = json.loads(payload.decode("utf-8"))
        file_sha256 = hashlib.sha256(payload).hexdigest()

        artifact = build_expert_guided_artifact(
            _RootOnlyBridge(),
            self._request(),
            seeds=(17,),
            horizon_ms=1000,
            profile_metadata={"kind": "test_profile"},
            expert_manifest=None,
            cat2_profile_snapshot=snapshot,
            cat2_profile_snapshot_file_sha256=file_sha256,
            cat2_profile_snapshot_path=str(DEFAULT_CAT2_PROFILE_SNAPSHOT),
        )

        contract = artifact["expert_contract"]
        expert_id = "cat2.fury.brainofcat_shadow.saved_profile_source_v1"
        self.assertEqual(contract["cat2_live_status"], "CURRENT_UNSEALED_SOURCE_PROFILE")
        self.assertIn(expert_id, contract["source_derived_experts"])
        self.assertEqual(contract["deployed_non_voting_sources"], [expert_id])
        self.assertNotIn(expert_id, contract["candidate_only_sources"])
        self.assertEqual(contract["exact_runtime_expert_count"], 0)
        self.assertFalse(contract["cat2_current_profile"]["exact_runtime"])
        self.assertFalse(
            contract["cat2_current_profile"]["eligible_for_independent_vote"]
        )

        receipt = artifact["content_addressed_inputs"]["cat2_profile_snapshot"]
        self.assertEqual(receipt["content_sha256"], file_sha256)
        self.assertEqual(receipt["content_hash_scope"], "EXACT_FILE_BYTES")
        self.assertEqual(receipt["authority_state"], "CURRENT_UNSEALED_SOURCE_PROFILE")
        self.assertEqual(
            receipt["profile_step_order"],
            [step["id"] for step in snapshot["profile"]["steps"]],
        )

        cat2_row = next(
            row
            for row in artifact["proposal_rows"][0]["experts"]
            if row["expert_id"] == expert_id
        )
        self.assertTrue(cat2_row["accepted"])
        self.assertEqual(cat2_row["source"]["provenance"]["role"], "DEPLOYED")
        self.assertEqual(
            cat2_row["source"]["provenance"]["kind"], "SOURCE_DERIVED"
        )
        self.assertFalse(cat2_row["source"]["eligible_for_independent_vote"])
        usage = artifact["cat2_profile_usage"]
        self.assertEqual(usage["proposal_row_count"], 1)
        self.assertEqual(usage["distinct_candidate_count"], 1)
        self.assertEqual(usage["exclusive_final_candidate_count"], 0)
        self.assertEqual(
            usage["search_guidance_effect"],
            "PROVENANCE_ONLY_NO_UNIQUE_FINAL_CANDIDATE",
        )
        self.assertFalse(usage["ranking_weight_applied"])
        self.assertFalse(usage["independent_vote_applied"])
        self.assertTrue(benchmark.called)

    @patch(
        "o2o_dps.fury_expert_guided_search_v1.benchmark_prefix_candidates",
        side_effect=_benchmark_stub,
    )
    def test_none_snapshot_preserves_legacy_unavailable_cat2_behavior(
        self, benchmark: object
    ) -> None:
        artifact = build_expert_guided_artifact(
            _RootOnlyBridge(),
            self._request(),
            seeds=(23,),
            horizon_ms=1000,
            profile_metadata={"kind": "test_profile"},
            expert_manifest=None,
            cat2_profile_snapshot=None,
        )

        contract = artifact["expert_contract"]
        self.assertEqual(contract["cat2_live_status"], "MISSING_NO_PROFILE")
        self.assertEqual(contract["deployed_non_voting_sources"], [])
        self.assertIsNone(contract["cat2_current_profile"])
        self.assertFalse(
            artifact["content_addressed_inputs"]["cat2_profile_snapshot"]["provided"]
        )
        legacy_row = next(
            row
            for row in artifact["proposal_rows"][0]["experts"]
            if row["expert_id"] == "cat2.fury.profile"
        )
        self.assertFalse(legacy_row["accepted"])
        self.assertEqual(
            artifact["cat2_profile_usage"],
            {
                "expert_id": None,
                "authority_state": "MISSING_NO_PROFILE",
                "proposal_row_count": 0,
                "distinct_candidate_count": 0,
                "exclusive_final_candidate_count": 0,
                "search_guidance_effect": "NOT_AVAILABLE_NO_PROFILE",
                "ranking_weight_applied": False,
                "independent_vote_applied": False,
            },
        )
        self.assertTrue(benchmark.called)


if __name__ == "__main__":
    unittest.main()
