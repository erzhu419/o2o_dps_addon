from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_ROOT = PROJECT_ROOT / "configs" / "wowsims"
SOURCE_PATH = CONFIG_ROOT / "fury_warrior_live.json"
PROFILE_PATH = CONFIG_ROOT / "fury_warrior_clean_dual.json"
METADATA_PATH = CONFIG_ROOT / "fury_warrior_clean_dual.metadata.json"


def _load(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"{path.name} must contain one JSON object")
    return value


def _player(request: dict[str, object]) -> dict[str, object]:
    return request["raid"]["parties"][0]["players"][0]


class FuryCleanDualProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = _load(SOURCE_PATH)
        cls.profile = _load(PROFILE_PATH)
        cls.metadata = _load(METADATA_PATH)

    def test_only_weapon_slots_differ_from_live_source(self) -> None:
        expected = deepcopy(self.source)
        expected_items = _player(expected)["equipment"]["items"]
        expected_items[14] = {"id": 18832}
        expected_items[15] = {"id": 19866}

        self.assertEqual(self.profile, expected)

    def test_weapon_pair_has_no_enchants_or_bonereaver(self) -> None:
        items = _player(self.profile)["equipment"]["items"]

        self.assertEqual(items[14], {"id": 18832})
        self.assertEqual(items[15], {"id": 19866})
        self.assertNotIn(17076, [item.get("id") for item in items])

    def test_interactive_single_target_contract_is_preserved(self) -> None:
        source_options = self.source["simOptions"]
        probe_options = self.profile["simOptions"]
        source_player = _player(self.source)
        probe_player = _player(self.profile)

        self.assertIs(probe_options["interactive"], True)
        self.assertEqual(probe_options, source_options)
        self.assertEqual(
            probe_player["warrior"]["options"]["startingRage"],
            source_player["warrior"]["options"]["startingRage"],
        )
        self.assertEqual(len(self.profile["encounter"]["targets"]), 1)

    def test_metadata_prevents_live_or_superiority_claims(self) -> None:
        self.assertEqual(
            self.metadata["status"],
            "mechanics_search_probe_not_live_loadout",
        )
        self.assertEqual(
            self.metadata["excluded_weapon"]["item_id"],
            17076,
        )
        self.assertIn(
            "armor-reduction",
            self.metadata["excluded_weapon"]["reason"],
        )
        self.assertEqual(
            self.metadata["calibration_links"]["locked_main_hand_item_id"],
            18832,
        )
        self.assertEqual(
            self.metadata["calibration_links"]["locked_off_hand_item_id"],
            19866,
        )
        self.assertGreater(len(self.metadata["simulator_blockers"]), 0)
        self.assertIs(
            self.metadata["gates"]["expert_superiority_claim_allowed"],
            False,
        )
        self.assertIs(
            self.metadata["gates"]["policy_deployment_allowed"],
            False,
        )


if __name__ == "__main__":
    unittest.main()
