from __future__ import annotations

import pickle
import unittest

from o2o_dps import chronicle_external_historical_fury_policy_v2 as policy_v2
from o2o_dps import chronicle_external_historical_fury_policy_v4 as policy_v4
from o2o_dps import chronicle_external_historical_fury_policy_v4_hpc_v1 as hpc_v4


def _view(prefix_value: str, queue_value: str) -> policy_v4.FeatureView:
    return policy_v4.FeatureView(
        v2_contexts={
            "coarse": f"coarse-{prefix_value}",
            "tactical": f"tactical-{prefix_value}",
            "full": f"full-{prefix_value}",
        },
        prefix_atoms=(
            policy_v4.FeatureAtom("base.wave_coarse", prefix_value),
            policy_v4.FeatureAtom("queue.prefix_observation", queue_value),
        ),
        dynamic=policy_v4.ObservedDynamicState(
            buckets={"rage": policy_v4.MISSING},
            atoms=(),
            missing_fields=("rage",),
            source="CURRENT_STAGE5_FIELD_ABSENT",
        ),
        provenance={"exact_inventory_used_as_model_context": False},
    )


class ChronicleExternalHistoricalFuryPolicyV4HpcTests(unittest.TestCase):
    def test_arm_a_generic_evaluator_reproduces_frozen_v2_learner(self) -> None:
        v2_components: dict[str, policy_v2._Aggregate] = {}
        v4_components: dict[str, policy_v4.AdditiveAggregate] = {}
        for index in range(6):
            component = f"component-{index}"
            old = policy_v2._Aggregate()
            new = policy_v4.AdditiveAggregate()
            for offset in range(10 + index):
                view = _view(str(index % 2), "NO_PENDING_QUEUE_START_OBSERVED")
                action_key = (
                    "warrior.bloodthirst"
                    if (offset + index) % 3
                    else "warrior.whirlwind"
                )
                action = (
                    f'{{"action_key":"{action_key}",'
                    '"target_role":"CURRENT_ENEMY"}'
                )
                old.add(view.v2_contexts, action)
                new.add(view, action, arm=policy_v4.ARM_A)
            v2_components[component] = old
            v4_components[component] = new
        expected = policy_v2._evaluate_component_split(
            v2_components,
            fold_count=5,
            split_seed=20260911,
            alpha=0.5,
            backoff_strength=8.0,
        )
        observed = hpc_v4.evaluate_component_split(
            v4_components,
            fold_count=5,
            split_seed=20260911,
            alpha=0.5,
            backoff_strength=8.0,
        )
        self.assertEqual(observed["component_to_fold"], expected["component_to_fold"])
        self.assertEqual(observed["metrics"], expected["metrics"])

    def test_current_stage5_c_is_exactly_b_and_aggregate_is_pickleable(self) -> None:
        b = policy_v4.AdditiveAggregate()
        c = policy_v4.AdditiveAggregate()
        for action, queue in (
            ('{"action_key":"warrior.heroic_strike","target_role":"CURRENT_ENEMY"}', "warrior.heroic_strike"),
            ('{"action_key":"warrior.cleave","target_role":"CURRENT_ENEMY"}', "warrior.cleave"),
        ):
            view = _view("wave", queue)
            b.add(view, action, arm=policy_v4.ARM_B)
            c.add(view, action, arm=policy_v4.ARM_C)
        self.assertEqual(b, c)
        self.assertEqual(pickle.loads(pickle.dumps(b)), b)

    def test_queue_group_is_reported_without_a_full_key(self) -> None:
        components: dict[str, policy_v4.AdditiveAggregate] = {}
        for index in range(4):
            aggregate = policy_v4.AdditiveAggregate()
            action = (
                '{"action_key":"warrior.heroic_strike","target_role":"CURRENT_ENEMY"}'
                if index % 2
                else '{"action_key":"warrior.cleave","target_role":"CURRENT_ENEMY"}'
            )
            aggregate.add(
                _view("wave", "warrior.heroic_strike"),
                action,
                arm=policy_v4.ARM_B,
            )
            components[f"component-{index}"] = aggregate
        result = hpc_v4.evaluate_component_split(
            components,
            fold_count=2,
            split_seed=20260911,
            alpha=0.5,
            backoff_strength=8.0,
        )
        queue = result["action_group_metrics"]["QUEUE_HS_CLEAVE"]
        self.assertEqual(queue["heldout_decision_count"], 4)
        self.assertIn("queue.prefix_observation", result["context_family_match_counts"])
        self.assertNotIn("static_character_profile", result["context_family_match_counts"])


if __name__ == "__main__":
    unittest.main()
