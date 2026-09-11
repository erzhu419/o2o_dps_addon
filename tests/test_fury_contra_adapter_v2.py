from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.expert_policy import ExpertRole, SwingQueueOp
from o2o_dps.fury_contra_adapter_v2 import (
    EVIDENCE_SCHEMA,
    SEMANTIC_ID,
    SOURCE_POLICY_ID,
    ContraDeployedFuryAdapterV2,
    ContraDeployedFuryStateV2,
    ContraEvidenceKindV2,
    ContraFieldEvidenceV2,
    ContraTargetClassificationV2,
    derive_zssdw_from_loadout,
)
from o2o_dps.fury_expert_adapters import (
    BLOODTHIRST,
    EXECUTE,
    FuryExpertState,
    WeaponMode,
)


SOURCE_SHA256 = "1" * 64
CORPUS_SHA256 = "2" * 64


def _evidence(
    kind: ContraEvidenceKindV2 = ContraEvidenceKindV2.OBSERVED_SOURCE,
) -> ContraFieldEvidenceV2:
    return ContraFieldEvidenceV2(
        kind=kind,
        source_sha256=SOURCE_SHA256,
        corpus_sha256=CORPUS_SHA256,
    )


def _state(**combat_changes: object) -> ContraDeployedFuryStateV2:
    combat = FuryExpertState(
        rage=0,
        target_health_pct=1,  # ignored by v2 evidence below
        weapon_mode=WeaponMode.TWO_HAND,
        target_is_boss=True,  # ignored; classification evidence is authoritative
    )
    if combat_changes:
        combat = replace(combat, **combat_changes)
    return ContraDeployedFuryStateV2(
        combat=combat,
        target_health_pct=50.0,
        target_max_health=60000,
        target_classification=ContraTargetClassificationV2.ELITE,
        target_name="卡拉赞精英",
        target_health_pct_evidence=_evidence(),
        target_max_health_evidence=_evidence(),
        target_classification_evidence=_evidence(),
        target_name_evidence=_evidence(),
        equipped_item_names=(),
        equipment_evidence=_evidence(ContraEvidenceKindV2.PINNED_STATIC_INPUT),
        target_position_evidence=_evidence(
            ContraEvidenceKindV2.SIMULATOR_STATE
        ),
    )


class FuryContraAdapterV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = ContraDeployedFuryAdapterV2()

    def test_semantic_identity_maps_to_the_same_deployed_source_policy(self) -> None:
        decision = self.adapter.propose(_state())

        self.assertTrue(decision.valid)
        self.assertEqual(decision.expert_id, SEMANTIC_ID)
        self.assertEqual(
            decision.metadata["semantic_parent_id"], SOURCE_POLICY_ID
        )
        self.assertEqual(decision.provenance.role, ExpertRole.DEPLOYED)
        self.assertTrue(decision.eligible_for_independent_vote)
        self.assertTrue(
            decision.provenance.authority_files[1].endswith(
                r"Contra\Contra_ALL.lua"
            )
        )
        self.assertIn(
            "Contra_ALL.lua:31612-31732", decision.provenance.source_refs
        )
        self.assertEqual(decision.metadata["evidence_contract"], EVIDENCE_SCHEMA)
        self.assertEqual(
            decision.metadata["field_evidence"]["target_max_health"],
            {
                "schema": EVIDENCE_SCHEMA,
                "kind": ContraEvidenceKindV2.OBSERVED_SOURCE.value,
                "source_sha256": SOURCE_SHA256,
                "corpus_sha256": CORPUS_SHA256,
                "hypothesis_id": None,
            },
        )

    def test_every_branch_input_and_provenance_is_fail_closed(self) -> None:
        base = _state()
        mutations = {
            "health": replace(base, target_health_pct=None),
            "health_evidence": replace(
                base, target_health_pct_evidence=None
            ),
            "max_health": replace(base, target_max_health=None),
            "max_health_evidence": replace(
                base,
                target_max_health_evidence=ContraFieldEvidenceV2(
                    ContraEvidenceKindV2.MISSING
                ),
            ),
            "classification": replace(base, target_classification=None),
            "classification_evidence": replace(
                base, target_classification_evidence=None
            ),
            "name": replace(base, target_name=None),
            "name_evidence": replace(base, target_name_evidence=None),
            "equipment": replace(base, equipped_item_names=None),
            "equipment_evidence": replace(
                base, equipment_evidence=None
            ),
            "position": replace(
                base,
                combat=replace(base.combat, target_distance_yards=float("nan")),
            ),
            "position_evidence": replace(
                base, target_position_evidence=None
            ),
        }
        for label, state in mutations.items():
            with self.subTest(label=label):
                decision = self.adapter.propose(state)
                self.assertFalse(decision.valid)
                self.assertFalse(decision.eligible_for_independent_vote)
                self.assertEqual(decision.raw_sink_order, ())
                self.assertTrue(decision.metadata["fail_closed"])
                self.assertTrue(decision.metadata["input_errors"])

        legacy = self.adapter.propose(base.combat)  # type: ignore[arg-type]
        self.assertFalse(legacy.valid)
        self.assertEqual(legacy.raw_sink_order, ())
        self.assertEqual(legacy.metadata["input_errors"], ["v2_state_missing"])

    def test_arbitrary_nonempty_strings_cannot_create_evidence_or_a_vote(self) -> None:
        base = _state()
        fields = (
            ("target_health_pct_evidence", "target_health_pct"),
            ("target_max_health_evidence", "target_max_health"),
            ("target_classification_evidence", "target_classification"),
            ("target_name_evidence", "target_name"),
            ("equipment_evidence", "equipped_item_names"),
            ("target_position_evidence", "target_position"),
        )
        for field, evidence_name in fields:
            with self.subTest(field=field):
                state = replace(base, **{field: "fabricated.nonempty.provenance"})
                decision = self.adapter.propose(state)
                self.assertFalse(decision.valid)
                self.assertFalse(decision.eligible_for_independent_vote)
                self.assertEqual(decision.raw_sink_order, ())
                self.assertIn(
                    f"{evidence_name}_evidence_missing_or_invalid",
                    decision.metadata["input_errors"],
                )

    def test_sensitivity_hypotheses_execute_but_never_vote(self) -> None:
        base = _state()
        fields = (
            ("target_health_pct_evidence", "target_health_pct"),
            ("target_max_health_evidence", "target_max_health"),
            ("target_classification_evidence", "target_classification"),
            ("target_name_evidence", "target_name"),
            ("equipment_evidence", "equipped_item_names"),
            ("target_position_evidence", "target_position"),
        )
        for field, field_name in fields:
            hypothesis_id = f"upper_karazhan.{field_name}.sensitivity.v1"
            sensitivity = ContraFieldEvidenceV2(
                ContraEvidenceKindV2.SENSITIVITY_HYPOTHESIS,
                hypothesis_id=hypothesis_id,
            )
            with self.subTest(field=field):
                decision = self.adapter.propose(replace(base, **{field: sensitivity}))
                self.assertTrue(decision.valid)
                self.assertFalse(decision.eligible_for_independent_vote)
                self.assertTrue(decision.metadata["sensitivity_only"])
                self.assertEqual(decision.metadata["sensitivity_fields"], [field_name])
                self.assertFalse(decision.metadata["independent_vote_allowed"])
                self.assertEqual(
                    decision.metadata["field_evidence"][field_name]["hypothesis_id"],
                    hypothesis_id,
                )

    def test_typed_evidence_rejects_unbound_or_malformed_authority(self) -> None:
        with self.assertRaisesRegex(ValueError, "source/corpus SHA-256"):
            ContraFieldEvidenceV2(ContraEvidenceKindV2.OBSERVED_SOURCE)
        with self.assertRaisesRegex(ValueError, "lowercase SHA-256"):
            ContraFieldEvidenceV2(
                ContraEvidenceKindV2.SIMULATOR_STATE,
                source_sha256="A" * 64,
            )
        with self.assertRaisesRegex(ValueError, "hypothesis_id"):
            ContraFieldEvidenceV2(
                ContraEvidenceKindV2.SENSITIVITY_HYPOTHESIS
            )
        with self.assertRaisesRegex(ValueError, "cannot carry"):
            ContraFieldEvidenceV2(
                ContraEvidenceKindV2.MISSING,
                source_sha256=SOURCE_SHA256,
            )

    def test_zssdw_is_derived_from_unique_equipped_names_not_v1_default(self) -> None:
        names = (
            "兄弟会头盔",
            "兄弟会胸甲",
            "兄弟会护腿",
            "兄弟会头盔",  # HasEquipItem is boolean; duplicates do not add twice.
            "削骨之刃",
        )
        self.assertEqual(derive_zssdw_from_loadout(names), 3)

        high_set = replace(
            _state(rage=35, contra_st_s=0.5),
            target_max_health=20000,
            equipped_item_names=names,
        )
        low_set = replace(
            high_set,
            equipped_item_names=("兄弟会头盔", "兄弟会胸甲"),
        )
        high_decision = self.adapter.propose(high_set)
        low_decision = self.adapter.propose(low_set)

        self.assertEqual(high_decision.metadata["derived_zssdw"], 3)
        self.assertEqual(low_decision.metadata["derived_zssdw"], 2)
        self.assertEqual(high_decision.swing_queue, SwingQueueOp.HEROIC_STRIKE)
        self.assertEqual(low_decision.swing_queue, SwingQueueOp.KEEP)

    def test_large_nonboss_preserves_raw_multi_sink_and_last_sink(self) -> None:
        state = replace(
            _state(
                rage=20,
                bloodthirst_ready_in_s=3.0,
                slam_remaining_s=0.5,
            ),
            target_health_pct=10.0,
        )
        decision = self.adapter.propose(state)

        gcd_sinks = [
            sink for sink in decision.raw_sink_order if sink.channel == "gcd"
        ]
        self.assertEqual([sink.value for sink in gcd_sinks], ["嗜血", "斩杀"])
        self.assertEqual(
            [sink.source_ref for sink in gcd_sinks],
            ["Contra_ALL.lua:31683", "Contra_ALL.lua:31684"],
        )
        self.assertEqual(decision.gcd, EXECUTE)
        self.assertEqual(decision.metadata["raw_gcd_calls"], [BLOODTHIRST, EXECUTE])
        channels = [sink.channel for sink in decision.raw_sink_order]
        self.assertLess(channels.index("cast_control"), channels.index("gcd"))

    def test_medium_worldboss_runs_both_independent_source_blocks(self) -> None:
        state = replace(
            _state(rage=40, contra_ss_s=2.0),
            target_health_pct=10.0,
            target_max_health=30000,
            target_classification=ContraTargetClassificationV2.WORLDBOSS,
            target_name="小血量世界首领",
        )
        decision = self.adapter.propose(state)

        execute_refs = [
            sink.source_ref
            for sink in decision.raw_sink_order
            if sink.channel == "gcd" and sink.value == "斩杀"
        ]
        self.assertEqual(
            execute_refs,
            ["Contra_ALL.lua:31653", "Contra_ALL.lua:31700"],
        )
        self.assertTrue(decision.metadata["source_is_boss"])
        self.assertTrue(
            decision.metadata["max_health_branches_are_independent_source_ifs"]
        )

    def test_training_dummy_uses_name_and_is_excluded_from_large_nonboss(self) -> None:
        state = replace(
            _state(bloodthirst_ready_in_s=5.0),
            target_classification=ContraTargetClassificationV2.NORMAL,
            target_name="学徒训练假人",
            target_max_health=60000,
        )
        decision = self.adapter.propose(state)

        bloodthirst_refs = [
            sink.source_ref
            for sink in decision.raw_sink_order
            if sink.channel == "gcd" and sink.value == "嗜血"
        ]
        self.assertEqual(bloodthirst_refs, ["Contra_ALL.lua:31628"])
        self.assertFalse(decision.metadata["source_is_boss"])
        self.assertTrue(decision.metadata["source_is_training_dummy"])

    def test_raid_target_preserves_tauren_and_or_precedence_bug(self) -> None:
        state = replace(
            _state(
                rage=0,
                flurry_active=False,
                race_is_tauren=True,
                target_distance_yards=5.0,
                bloodthirst_ready_in_s=0.0,
            ),
            target_health_pct=80.0,
            target_max_health=60000,
            target_classification=ContraTargetClassificationV2.WORLDBOSS,
            target_name="世界首领",
        )
        decision = self.adapter.propose(state)

        # xuanfeng is frozen false.  Because the Lua condition is
        # `(xuanfeng and non_tauren_range) or tauren_range`, it still emits WW,
        # then both source Bloodthirst sinks.  The final sink remains the
        # normalized decision while all three are retained in raw order.
        self.assertEqual(
            [
                (sink.value, sink.source_ref)
                for sink in decision.raw_sink_order
                if sink.channel == "gcd"
            ],
            [
                ("旋风斩", "Contra_ALL.lua:31618"),
                ("嗜血", "Contra_ALL.lua:31621"),
                ("嗜血", "Contra_ALL.lua:31628"),
            ],
        )
        self.assertEqual(decision.gcd, BLOODTHIRST)

    def test_classification_evidence_overrides_legacy_boss_default(self) -> None:
        # FuryExpertState.target_is_boss defaults true, but deployed Contra only
        # treats UnitClassification == worldboss as IsBoss.
        state = _state(bloodthirst_ready_in_s=5.0)
        decision = self.adapter.propose(state)

        self.assertFalse(decision.metadata["source_is_boss"])
        self.assertTrue(decision.metadata["legacy_combat_target_flags_ignored"])
        self.assertEqual(
            [
                sink.source_ref
                for sink in decision.raw_sink_order
                if sink.channel == "gcd"
            ],
            ["Contra_ALL.lua:31672"],
        )

    def test_max_health_boundaries_select_large_medium_and_small_source_refs(self) -> None:
        base = replace(
            _state(rage=90, contra_st_s=0.5, bloodthirst_ready_in_s=0.0),
            equipped_item_names=(),
        )
        expected_queue_ref = {
            51000: "Contra_ALL.lua:31668",
            25000: "Contra_ALL.lua:31694",
            24999: "Contra_ALL.lua:31717",
        }
        for max_health, source_ref in expected_queue_ref.items():
            with self.subTest(max_health=max_health):
                decision = self.adapter.propose(
                    replace(base, target_max_health=max_health)
                )
                queue_refs = [
                    sink.source_ref
                    for sink in decision.raw_sink_order
                    if sink.channel == "swing_queue"
                ]
                self.assertEqual(queue_refs, [source_ref])


if __name__ == "__main__":
    unittest.main()
