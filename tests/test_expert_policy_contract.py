from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.expert_policy import (
    WAIT_ACTION,
    CastControl,
    ExpertDecision,
    ExpertProvenance,
    ExpertRole,
    ProvenanceKind,
    RawSink,
    StanceOp,
    SwingQueueOp,
    TargetOp,
)


class ExpertPolicyContractTests(unittest.TestCase):
    def _provenance(
        self, role: ExpertRole = ExpertRole.DEPLOYED
    ) -> ExpertProvenance:
        return ExpertProvenance(
            expert_id="fixture.expert",
            kind=ProvenanceKind.SOURCE_DERIVED,
            role=role,
            authority_files=("fixture/Policy.lua",),
            source_refs=("Policy.lua:10-20",),
        )

    def test_serializes_all_factorized_lanes_and_preserves_raw_order(self) -> None:
        decision = ExpertDecision(
            provenance=self._provenance(),
            valid=True,
            gcd="warrior.execute",
            wait_ms=None,
            swing_queue=SwingQueueOp.CANCEL,
            off_gcd=("warrior.bloodrage", "item.slot13"),
            stance=StanceOp.BERSERKER,
            target=TargetOp.NEAREST_ENEMY,
            cast_control=CastControl.STOP_CAST,
            raw_sink_order=(
                RawSink("target", "TargetNearestEnemy", source_ref="Policy.lua:10"),
                RawSink("cast_control", "SpellStopCasting", source_ref="Policy.lua:11"),
                RawSink("off_gcd", "CastSpellByName", "血性狂暴", "Policy.lua:12"),
                RawSink("swing_queue", "CANCEL", source_ref="Policy.lua:13"),
                RawSink("gcd", "CastSpellByName", "斩杀", "Policy.lua:14"),
            ),
            eligible_for_independent_vote=True,
            reason="fixture",
        )

        document = decision.to_dict()
        self.assertEqual(document["gcd"], {"action": "warrior.execute", "wait_ms": None})
        self.assertEqual(document["swing_queue"], "CANCEL")
        self.assertEqual(
            document["off_gcd"], ["warrior.bloodrage", "item.slot13"]
        )
        self.assertEqual(document["stance"], "BERSERKER")
        self.assertEqual(document["target"], "NEAREST_ENEMY")
        self.assertEqual(document["cast_control"], "STOP_CAST")
        self.assertEqual(
            [sink["channel"] for sink in document["raw_sink_order"]],
            ["target", "cast_control", "off_gcd", "swing_queue", "gcd"],
        )
        self.assertEqual(document["provenance"]["kind"], "SOURCE_DERIVED")

    def test_wait_is_explicit_and_spell_cannot_also_wait(self) -> None:
        wait = ExpertDecision(
            provenance=self._provenance(),
            valid=True,
            gcd=WAIT_ACTION,
            wait_ms=125,
            eligible_for_independent_vote=True,
        )
        self.assertTrue(wait.is_wait)
        self.assertEqual(wait.to_dict()["gcd"]["wait_ms"], 125)

        with self.assertRaisesRegex(ValueError, "wait_ms=None"):
            ExpertDecision(
                provenance=self._provenance(),
                valid=True,
                gcd="warrior.bloodthirst",
                wait_ms=100,
            )

    def test_candidate_and_invalid_results_cannot_be_independent_votes(self) -> None:
        with self.assertRaisesRegex(ValueError, "only a deployed expert"):
            ExpertDecision(
                provenance=self._provenance(ExpertRole.CANDIDATE),
                valid=True,
                eligible_for_independent_vote=True,
            )
        with self.assertRaisesRegex(ValueError, "invalid proposal"):
            ExpertDecision(
                provenance=self._provenance(),
                valid=False,
                eligible_for_independent_vote=True,
            )

    def test_local_manifest_records_authority_without_probing_external_paths(self) -> None:
        manifest_path = PROJECT_ROOT / "configs" / "experts" / "fury_experts_v1.json"
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
        by_id = {entry["expert_id"]: entry for entry in document["experts"]}

        self.assertEqual(document["schema"], "fury_expert_manifest/v1")
        self.assertTrue(
            by_id["contra.deployed.fury.raid_a"]["authority"]["toc"].endswith(
                r"Contra\Contra.toc"
            )
        )
        self.assertTrue(
            by_id["contra.deployed.fury.raid_a"]["authority"]["loaded_policy"].endswith(
                r"Contra\Contra_ALL.lua"
            )
        )
        self.assertNotIn(
            "Contra.lua",
            by_id["contra.deployed.fury.raid_a"]["authority"]["loaded_policy"],
        )
        self.assertFalse(
            by_id["contra_new.fury.candidate"]["eligible_for_independent_vote"]
        )
        current_cat2 = by_id[
            "cat2.fury.brainofcat_shadow.saved_profile_source_v1"
        ]
        self.assertEqual(current_cat2["role"], "DEPLOYED")
        self.assertEqual(current_cat2["provenance_kind"], "SOURCE_DERIVED")
        self.assertEqual(
            current_cat2["availability"], "CURRENT_UNSEALED_SOURCE_PROFILE"
        )
        self.assertFalse(current_cat2["eligible_for_independent_vote"])
        self.assertFalse(current_cat2["source_execution"])
        self.assertFalse(current_cat2["exact_runtime"])
        self.assertEqual(
            current_cat2["snapshot"]["file_sha256"],
            "e5c17c343a46f14565a413920e635e455ad161654fb3c9aab6d9db8efff8875a",
        )
        self.assertEqual(
            current_cat2["snapshot"]["profile_semantic_sha256"],
            "891b2ffda747f0c4202c8de874f18e599206c78df1608bcf1eb06c5aa49d0625",
        )
        self.assertEqual(
            current_cat2["snapshot"]["source_bundle_sha256"],
            "1d591e5ef60222a0f6eb1e081b4f43feb4cfa1576103558689dc6fda2aa84970",
        )
        self.assertEqual(
            current_cat2["profile"]["step_order"],
            [
                "warrior_o2o_policy_brain",
                "common_auto_attack",
                "warrior_berserker_stance",
                "warrior_bloodrage",
                "warrior_execute",
                "warrior_bloodthirst",
                "warrior_whirlwind",
                "warrior_heroic_strike_alt",
            ],
        )
        self.assertEqual(
            by_id["cat2.fury.curated_candidate"]["availability"],
            "EXPLICIT_CURATED_CARD_STACK",
        )
        self.assertFalse(
            by_id["cat2.fury.curated_candidate"]["eligible_for_independent_vote"]
        )


if __name__ == "__main__":
    unittest.main()
