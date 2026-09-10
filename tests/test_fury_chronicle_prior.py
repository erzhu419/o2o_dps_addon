from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.fury_chronicle_prior import (
    DEFAULT_MODEL,
    PRIOR_ROLE,
    FuryChroniclePrior,
    FuryChroniclePriorError,
)


LABELS = ["warrior.slam", "warrior.whirlwind", "warrior.execute"]


def _model_document() -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "chronicle_fury_partial_observation_behavior_prior",
        "analysis_only": True,
        "training_contract": {
            "label_semantics": (
                "next uniquely linked successful server START candidate"
            ),
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
                "order_1": [
                    {
                        "context": {
                            "last_auto_attack_elapsed_bucket": "MISSING",
                            "recent_action_tokens": ["id:1680:succeeded"],
                        },
                        "counts": {
                            "warrior.slam": 1,
                            "warrior.execute": 3,
                        },
                    },
                    {
                        "context": {
                            "last_auto_attack_elapsed_bucket": "OBSERVED_LT_1000_MS",
                            "recent_action_tokens": ["id:1680:succeeded"],
                        },
                        "counts": {"warrior.execute": 2},
                    },
                ],
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


def _write_model(root: Path, document: dict[str, object] | None = None) -> Path:
    path = root / "model.json"
    path.write_text(
        json.dumps(document or _model_document()),
        encoding="utf-8",
    )
    return path


class FuryChroniclePriorTests(unittest.TestCase):
    def test_order_two_proposal_matches_training_smoothing_and_card_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            model = _write_model(Path(temporary_directory))
            result = FuryChroniclePrior(model).propose(
                [
                    "id:23894:succeeded",
                    "id:1680:succeeded",
                ],
                "MISSING",
                top_k=2,
            )

        self.assertEqual(result["prior_role"], PRIOR_ROLE)
        self.assertTrue(result["analysis_only"])
        self.assertFalse(result["deployment_allowed"])
        self.assertEqual(result["fallback_layer"], "order_2")
        self.assertEqual(result["context_support"], 5)
        self.assertEqual(
            [proposal["action_key"] for proposal in result["proposals"]],
            ["warrior.whirlwind", "warrior.slam"],
        )
        self.assertEqual(result["proposals"][0]["cat2_card_id"], "warrior_whirlwind")
        self.assertEqual(result["proposals"][0]["support_count"], 4)
        self.assertAlmostEqual(result["proposals"][0]["probability"], 4.5 / 6.5)

    def test_backoff_uses_order_one_then_global_below_minimum_support(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            prior = FuryChroniclePrior(_write_model(Path(temporary_directory)))
            order_one = prior.propose(
                ["id:999:succeeded", "id:1680:succeeded"],
                "MISSING",
            )
            global_result = prior.propose(
                ["id:1680:succeeded"],
                "OBSERVED_LT_1000_MS",
            )

        self.assertEqual(order_one["fallback_layer"], "order_1")
        self.assertEqual(order_one["context_support"], 4)
        self.assertEqual(order_one["proposals"][0]["action_key"], "warrior.execute")
        self.assertEqual(global_result["fallback_layer"], "global")
        self.assertEqual(global_result["context_support"], 10)
        self.assertEqual(global_result["matched_context"]["recent_successful_action_tokens"], [])
        self.assertEqual(global_result["proposals"][0]["action_key"], "warrior.slam")

    def test_only_last_two_successful_tokens_are_features(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            prior = FuryChroniclePrior(_write_model(Path(temporary_directory)))
            result = prior.propose(
                [
                    "id:999:succeeded",
                    "id:23894:succeeded",
                    "id:1680:succeeded",
                ],
                "MISSING",
            )

        self.assertEqual(result["fallback_layer"], "order_2")
        self.assertEqual(
            result["requested_context"]["recent_successful_action_tokens"],
            ["id:23894:succeeded", "id:1680:succeeded"],
        )

    def test_rejects_nonpartial_model_and_noncanonical_query(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            document = _model_document()
            document["analysis_only"] = False
            with self.assertRaisesRegex(FuryChroniclePriorError, "analysis_only"):
                FuryChroniclePrior(_write_model(root, document))

            prior = FuryChroniclePrior(_write_model(root))
            with self.assertRaisesRegex(FuryChroniclePriorError, ":succeeded"):
                prior.propose(["id:1680:failed"], "MISSING")
            with self.assertRaisesRegex(FuryChroniclePriorError, "model bucket"):
                prior.propose([], "UNKNOWN_BUCKET")
            with self.assertRaisesRegex(FuryChroniclePriorError, "top_k"):
                prior.propose([], "MISSING", top_k=0)

    def test_published_model_loads_as_partial_prior(self) -> None:
        result = FuryChroniclePrior(DEFAULT_MODEL).propose(
            ["id:1680:succeeded"],
            "MISSING",
            top_k=3,
        )
        self.assertEqual(result["prior_role"], PRIOR_ROLE)
        self.assertEqual(len(result["proposals"]), 3)
        self.assertTrue(
            all(proposal["cat2_card_id"] for proposal in result["proposals"])
        )
        self.assertIn(result["fallback_layer"], {"order_1", "global"})
        self.assertIn("causal reward or Q values", result["does_not_provide"])


if __name__ == "__main__":
    unittest.main()
