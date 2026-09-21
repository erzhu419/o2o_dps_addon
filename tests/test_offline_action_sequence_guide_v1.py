from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps import historical_behavior_clone_simulator_adapter_v2 as clone_adapter_v2
from o2o_dps import historical_behavior_clone_v2 as clone_v2
from o2o_dps.offline_action_sequence_guide_v1 import (
    BACKEND_CHRONICLE_PRIOR,
    BACKEND_CLONE_V2,
    SOURCE_BUILD_EXACT,
    ExactBuildIdentityV1,
    OfflineActionSequenceGuideV1,
    OfflineGuideContextV1,
    load_offline_action_sequence_guide_v1,
)
from o2o_dps.sim_bridge import ActionRef, AvailableAction
from tests.test_historical_behavior_clone_v2 import _binding, _prototype, _records


LABELS = ["warrior.slam", "warrior.whirlwind", "warrior.execute"]


def _prior_document() -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "chronicle_fury_partial_observation_behavior_prior",
        "analysis_only": True,
        "training_contract": {
            "label_semantics": "next uniquely linked successful server START candidate",
            "claims_excluded": [
                "full-state behavior cloning",
                "offline reinforcement learning",
                "DPS improvement",
                "deployable WoW policy",
            ],
        },
        "action_mapping": {
            "policy_action_key_to_cat2_card_id": {
                "warrior.slam": "warrior_slam",
                "warrior.whirlwind": "warrior_whirlwind",
                "warrior.execute": "warrior_execute",
            },
            "mapping_complete": True,
            "deployment_allowed": False,
        },
        "model": {
            "max_order": 2,
            "minimum_context_count": 3,
            "additive_smoothing_alpha": 0.5,
            "label_vocabulary": LABELS,
            "global_counts": {
                "warrior.slam": 6,
                "warrior.whirlwind": 3,
                "warrior.execute": 1,
            },
            "contexts": {
                "order_1": [],
                "order_2": [
                    {
                        "context": {
                            "last_auto_attack_elapsed_bucket": "MISSING",
                            "recent_action_tokens": [
                                "id:23894:succeeded",
                                "id:1680:succeeded",
                            ],
                        },
                        "counts": {
                            "warrior.whirlwind": 4,
                            "warrior.slam": 1,
                        },
                    }
                ],
            },
        },
    }


def _write_prior(root: Path) -> Path:
    path = root / "prior.json"
    path.write_text(json.dumps(_prior_document()), encoding="utf-8")
    return path


def _available(
    action_key: str,
    *,
    index: int,
    legal: bool = True,
) -> AvailableAction:
    binding = clone_adapter_v2.ACTION_SINK_BINDINGS_V2[action_key]
    return AvailableAction(
        index=index,
        action=binding.action_ref,
        label=action_key,
        legal=legal,
        ready_in_ms=0,
        triggers_gcd=binding.triggers_gcd,
    )


def _unknown_available(index: int) -> AvailableAction:
    return AvailableAction(
        index=index,
        action=ActionRef(item_id=99999),
        label="search-only-item",
        legal=True,
        ready_in_ms=0,
        triggers_gcd=False,
    )


def _clone_observation() -> dict[str, object]:
    return {
        "schema": clone_adapter_v2.OBSERVATION_SCHEMA,
        "simulator_time_ms": 500,
        "scenario_elapsed_ms": 500,
        "observed_target_count": 1,
        "observed_dead_target_count": 0,
        "actor_has_last_target": False,
        "last_controllable_action": None,
        "last_controllable_lane": None,
        "last_gcd_action_age_ms": None,
        "observed_stance": None,
        "prefix_observation_status": "OBSERVED_FROM_WAVE_ANCHOR",
    }


class OfflineActionSequenceGuideV1Tests(unittest.TestCase):
    def test_prior_orders_every_legal_exact_action_without_filtering_search(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            guide = load_offline_action_sequence_guide_v1(
                _write_prior(Path(temporary_directory))
            )
            result = guide.rank_available_actions(
                OfflineGuideContextV1(
                    recent_successful_action_history=(
                        "warrior.bloodthirst",
                        "warrior.whirlwind",
                    ),
                ),
                [
                    _available("warrior.execute", index=4),
                    _available("warrior.slam", index=2, legal=False),
                    _unknown_available(index=8),
                    _available("warrior.whirlwind", index=3),
                ],
            )

        self.assertEqual(BACKEND_CHRONICLE_PRIOR, result.model["backend"])
        self.assertEqual(
            [
                "warrior.whirlwind",
                "warrior.execute",
                None,
            ],
            [proposal.action_key for proposal in result.proposals],
        )
        self.assertEqual(3, len(result.ordered_action_refs))
        self.assertEqual(ActionRef(item_id=99999), result.ordered_action_refs[-1])
        self.assertAlmostEqual(0.9, result.proposals[0].probability)
        self.assertAlmostEqual(0.1, result.proposals[1].probability)
        self.assertEqual(0.0, result.proposals[2].probability)
        self.assertFalse(result.proposals[2].guide_supported)
        wire = result.to_dict()
        self.assertFalse(wire["contract"]["guide_filters_search_action_universe"])
        self.assertFalse(
            wire["contract"]["client_next_swing_queue_intent_reconstructed"]
        )
        self.assertFalse(wire["contract"]["target_switch_intent_reconstructed"])
        self.assertFalse(
            result.build_match_receipt.same_build_comparison_eligible
        )

    def test_build_receipt_requires_exact_complete_identity_equality(self) -> None:
        identity = {
            "equipment_items": [{"id": 19364, "enchant": 250}],
            "talents_string": "fury-exact-v1",
            "consumes": {"flask": 13512},
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            guide = OfflineActionSequenceGuideV1.from_chronicle_prior(
                _write_prior(Path(temporary_directory)),
                source_build_identity=ExactBuildIdentityV1(identity),
                source_build_scope=SOURCE_BUILD_EXACT,
            )
            matching = guide.rank_available_actions(
                OfflineGuideContextV1(evaluation_build_identity=identity),
                [_available("warrior.execute", index=0)],
            )
            mismatch = guide.rank_available_actions(
                OfflineGuideContextV1(
                    evaluation_build_identity={
                        **identity,
                        "talents_string": "different-build",
                    }
                ),
                [_available("warrior.execute", index=0)],
            )
            pooled = OfflineActionSequenceGuideV1.from_chronicle_prior(
                guide.model_path,
                source_build_identity=identity,
            ).rank_available_actions(
                OfflineGuideContextV1(evaluation_build_identity=identity),
                [_available("warrior.execute", index=0)],
            )

        self.assertTrue(
            matching.build_match_receipt.same_build_comparison_eligible
        )
        self.assertEqual(
            "EXACT_SOURCE_AND_EVALUATION_BUILD_IDENTITIES_MATCH",
            matching.build_match_receipt.reason,
        )
        self.assertFalse(
            mismatch.build_match_receipt.same_build_comparison_eligible
        )
        self.assertEqual(
            "EXACT_BUILD_IDENTITY_MISMATCH",
            mismatch.build_match_receipt.reason,
        )
        self.assertFalse(pooled.build_match_receipt.same_build_comparison_eligible)
        self.assertEqual(
            "SOURCE_IS_POOLED_OR_CROSS_BUILD_GUIDE",
            pooled.build_match_receipt.reason,
        )

    def test_clone_v2_reuses_validated_mark_head_and_keeps_zero_mass_rows(self) -> None:
        model = clone_v2.compile_weighted_prototype_v2(
            _prototype(), _records(), source_binding=_binding()
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "clone.model.json"
            path.write_text(
                json.dumps(model, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            guide = OfflineActionSequenceGuideV1.from_artifact(path)
            result = guide.rank_available_actions(
                OfflineGuideContextV1(observable_context=_clone_observation()),
                [
                    _available("warrior.whirlwind", index=1),
                    _unknown_available(index=2),
                    _available("warrior.bloodthirst", index=0),
                ],
            )

        self.assertEqual(BACKEND_CLONE_V2, result.model["backend"])
        self.assertEqual("fixture_prototype", result.prototype["prototype_id"])
        self.assertEqual(
            ["warrior.bloodthirst", "warrior.whirlwind", None],
            [proposal.action_key for proposal in result.proposals],
        )
        self.assertEqual(1.0, result.proposals[0].probability)
        self.assertEqual(0.0, result.proposals[1].probability)
        self.assertEqual(0.0, result.proposals[2].probability)
        provenance = result.proposals[0].to_dict()["provenance"]
        self.assertEqual("warrior.bloodthirst", provenance["action_key"])
        self.assertEqual("fixture_prototype", provenance["prototype"]["prototype_id"])
        self.assertFalse(
            provenance["build_match"]["same_build_comparison_eligible"]
        )


if __name__ == "__main__":
    unittest.main()
