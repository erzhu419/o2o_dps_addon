import copy
import json
from pathlib import Path
import unittest

from o2o_dps.development_white6603_inner_selection_v8 import build_inner_selection_v8


MANIFEST = Path(__file__).resolve().parents[1] / "configs/evaluation/development_white6603_v8_inner_selection.json"


class White6603InnerSelectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.frozen = json.loads(MANIFEST.read_text(encoding="utf-8"))
        component = cls.frozen["train_component_id"]
        cls.dispatch = {"tasks": [
            {"instance_id": instance_id, "component_id": component, "split": "TRAIN"}
            for instance_id in cls.frozen["fit_instance_ids"] + cls.frozen["selection_instance_ids"]
        ] + [
            {**task, "split": "VALIDATION"}
            for task in cls.frozen["external_validation_tasks"]
        ]}

    def test_frozen_membership_and_order_invariance(self):
        actual = build_inner_selection_v8(self.dispatch, self.frozen["source_dispatch"])
        self.assertEqual(actual, self.frozen)
        reversed_dispatch = {"tasks": list(reversed(self.dispatch["tasks"]))}
        self.assertEqual(build_inner_selection_v8(reversed_dispatch, self.frozen["source_dispatch"]), self.frozen)
        self.assertEqual((actual["fit_task_count"], actual["selection_task_count"]), (52, 13))
        self.assertEqual(actual["status"], "INTRA_COMPONENT_SELECTION_NOT_INDEPENDENT")

    def test_validation_component_cannot_enter_train(self):
        changed = copy.deepcopy(self.dispatch)
        changed["tasks"][0]["component_id"] = changed["tasks"][-1]["component_id"]
        with self.assertRaisesRegex(ValueError, "component boundary"):
            build_inner_selection_v8(changed, "frozen")

    def test_entire_raid_task_is_the_selection_unit(self):
        selected = set(self.frozen["selection_instance_ids"])
        fit = set(self.frozen["fit_instance_ids"])
        external = {task["instance_id"] for task in self.frozen["external_validation_tasks"]}
        self.assertFalse(selected & fit or selected & external or fit & external)
        self.assertEqual(len(selected | fit | external), 68)


if __name__ == "__main__":
    unittest.main()
