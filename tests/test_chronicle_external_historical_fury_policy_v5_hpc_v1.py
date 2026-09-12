from __future__ import annotations

from collections import Counter
import json
import os
from pathlib import Path
import pickle
import shutil
import tempfile
import unittest
from unittest import mock

from o2o_dps import chronicle_external_historical_fury_policy_v2 as policy_v2
from o2o_dps import chronicle_external_historical_fury_policy_v4 as policy_v4
from o2o_dps import chronicle_external_historical_fury_policy_v5_hpc_v1 as hpc_v5


ACTION_A = '{"action_key":"warrior.bloodthirst","target_role":"CURRENT_ENEMY"}'
ACTION_B = '{"action_key":"warrior.whirlwind","target_role":"CURRENT_ENEMY"}'


def _component(action_a: int, action_b: int) -> policy_v4.AdditiveAggregate:
    aggregate = policy_v4.AdditiveAggregate()
    atoms = tuple(
        policy_v4.FeatureAtom(f"v2.{level}", f"{level}-shared")
        for level in policy_v2.CONTEXT_LEVELS
    )
    pairs = tuple((atom.family, atom.value) for atom in atoms)
    for action, count in ((ACTION_A, action_a), (ACTION_B, action_b)):
        aggregate.decision_cells[(pairs, action)] = count
        aggregate.action_counts[action] = count
        for atom in atoms:
            values = aggregate.context_counts.setdefault(atom.family, {})
            values.setdefault(atom.value, Counter())[action] = count
    return aggregate


class ChronicleExternalHistoricalFuryPolicyV5HpcTests(unittest.TestCase):
    def test_maps_read_one_v4_aggregate_and_reduce_never_reopens_v4(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            v4_work = root / "v4-work"
            shards = v4_work / "shards"
            shards.mkdir(parents=True)
            components = {
                "component-a": _component(30000, 30000),
                "component-b": _component(31000, 29000),
                "component-c": _component(31241, 31240),
            }
            tasks = []
            for position in range(hpc_v5.EXPECTED_WORKER_COUNT):
                instance_id = f"instance-{position:03d}"
                tasks.append(
                    {
                        "position": position,
                        "instance_id": instance_id,
                    }
                )
                payload = {
                    "plan_id": hpc_v5.EXPECTED_V4_PLAN_ID,
                    "instance_id": instance_id,
                    "by_arm": {
                        policy_v4.ARM_A: components if position == 0 else {}
                    },
                }
                with (shards / f"{position:03d}.aggregate.pkl").open("wb") as handle:
                    pickle.dump(payload, handle, protocol=pickle.HIGHEST_PROTOCOL)
                receipt = {
                    "status": "COMPLETE",
                    "plan_id": hpc_v5.EXPECTED_V4_PLAN_ID,
                    "position": position,
                    "instance_id": instance_id,
                }
                (shards / f"{position:03d}.done.json").write_text(
                    json.dumps(receipt), encoding="utf-8"
                )
            v4_plan = {
                "plan_id": hpc_v5.EXPECTED_V4_PLAN_ID,
                "status": "PREPARED_NOT_EXECUTED",
                "revision": "paired_abc_low_card_prefix_external84_v1",
                "work_directory": str(v4_work),
                "fixed_parameters": {
                    "fold_count": 5,
                    "split_seed": 20260911,
                    "smoothing_alpha": 0.5,
                    "backoff_strength": 8.0,
                    "hyperparameter_search": False,
                },
                "tasks": tasks,
            }
            v4_plan_path = root / "v4-plan.json"
            v4_plan_path.write_text(json.dumps(v4_plan), encoding="utf-8")
            plan_path, plan = hpc_v5.create_plan(
                v4_plan_path=v4_plan_path,
                output_directory=root / "v5-output",
                source_root=project_root,
            )
            with mock.patch.dict(os.environ, {"GOMAXPROCS": "1"}):
                for task in plan["tasks"]:
                    hpc_v5.run_worker(
                        plan_path=plan_path,
                        position=task["position"],
                        node=task["node"],
                    )
                self.assertEqual(hpc_v5.status(plan_path=plan_path)["complete"], 84)
                shutil.rmtree(shards)
                reduction = hpc_v5.reduce(plan_path=plan_path)
            self.assertEqual(
                reduction["accounting"]["strict_controllable_labels"], 182481
            )
            self.assertEqual(reduction["accounting"]["outer_component_count"], 3)
            self.assertEqual(reduction["source"]["stage5_partitions_read"], 0)
            self.assertEqual(
                reduction["paired_evaluation"]["effective_fold_count"], 3
            )
            self.assertEqual(
                reduction["paired_evaluation"]["baseline_v2"]["top1_accuracy"],
                reduction["paired_evaluation"]["calibrated_v5"]["top1_accuracy"],
            )
            self.assertEqual(
                reduction["paired_evaluation"]["baseline_v2"]["top3_accuracy"],
                reduction["paired_evaluation"]["calibrated_v5"]["top3_accuracy"],
            )


if __name__ == "__main__":
    unittest.main()
