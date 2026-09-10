from __future__ import annotations

from copy import deepcopy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.wowsims_profile import (
    ProfileSelection,
    WowsimsProfileError,
    build_wowsims_profile,
    main,
    parse_item_link,
    select_profile_record,
    warrior_talents_to_string,
)


TEMPLATE_PATH = PROJECT_ROOT / "configs" / "wowsims" / "fury_warrior_phase1.json"


def _real_character_talents() -> list[dict[str, object]]:
    return [
        {"tab": 1, "index": 1, "tier": 1, "column": 1, "rank": 3, "maxRank": 3, "name": "强化英勇打击"},
        {"tab": 1, "index": 2, "tier": 1, "column": 2, "rank": 5, "maxRank": 5, "name": "战术掌握"},
        {"tab": 1, "index": 3, "tier": 1, "column": 3, "rank": 2, "maxRank": 2, "name": "强化撕裂"},
        {"tab": 1, "index": 8, "tier": 3, "column": 2, "rank": 2, "maxRank": 2, "name": "强化压制"},
        {"tab": 1, "index": 9, "tier": 3, "column": 3, "rank": 3, "maxRank": 3, "name": "重伤"},
        {"tab": 1, "index": 10, "tier": 4, "column": 2, "rank": 3, "maxRank": 3, "name": "双手武器专精"},
        {"tab": 1, "index": 11, "tier": 4, "column": 3, "rank": 2, "maxRank": 2, "name": "穿刺"},
        {"tab": 2, "index": 2, "tier": 1, "column": 3, "rank": 5, "maxRank": 5, "name": "残忍"},
        {"tab": 2, "index": 4, "tier": 2, "column": 3, "rank": 5, "maxRank": 5, "name": "怒不可遏"},
        {"tab": 2, "index": 5, "tier": 3, "column": 1, "rank": 5, "maxRank": 5, "name": "强化怒吼"},
        {"tab": 2, "index": 9, "tier": 4, "column": 3, "rank": 5, "maxRank": 5, "name": "狂怒"},
        {"tab": 2, "index": 11, "tier": 5, "column": 1, "rank": 2, "maxRank": 3, "name": "碾碎"},
        {"tab": 2, "index": 12, "tier": 5, "column": 2, "rank": 1, "maxRank": 1, "name": "死亡之愿"},
        {"tab": 2, "index": 13, "tier": 5, "column": 4, "rank": 2, "maxRank": 2, "name": "强化斩杀"},
        {"tab": 2, "index": 15, "tier": 6, "column": 2, "rank": 5, "maxRank": 5, "name": "乱舞"},
        {"tab": 2, "index": 17, "tier": 7, "column": 2, "rank": 1, "maxRank": 1, "name": "嗜血"},
    ]


def _state(item_id: int = 21679, *, static: bool = True) -> dict[str, object]:
    state: dict[str, object] = {
        "playerLevel": 60,
        "classFile": "WARRIOR",
        "equipment": [
            {"slot": 1, "link": "|cffa335ee|Hitem:21329:0:0:0|h[Head]|h|r"},
            {"slot": 3, "link": "|cffa335ee|Hitem:21330:3038:0:0|h[Shoulder]|h|r"},
            {"slot": 16, "link": f"|cffa335ee|Hitem:{item_id}:0:0:0|h[Weapon]|h|r"},
        ],
        "talents": _real_character_talents(),
    }
    if static:
        state.update(
            {
                "characterIdentity": {
                    "name": "TestWarrior",
                    "level": 60,
                    "raceFile": "Gnome",
                    "classFile": "WARRIOR",
                },
                "spellbook": [{"name": "猛击", "rank": "等级 5"}],
                "actionBarSpells": [{"slot": 1, "spellID": 45961}],
                "skillLines": [{"name": "双手剑", "rank": 305}],
                "bagItems": [{"bag": 0, "slot": 1, "link": "|Hitem:13446:0:0:0|h[x]|h"}],
            }
        )
    return state


class WowsimsProfileTests(unittest.TestCase):
    def test_real_turtle_talents_map_semantically_and_ravager_uses_option(self) -> None:
        talents_string, unmapped, trees, options, option_mappings = warrior_talents_to_string(
            _real_character_talents()
        )

        self.assertEqual(trees["arms"], "30205020332")
        self.assertEqual(trees["fury"], "05050005025010051")
        self.assertEqual(talents_string, "30205020332-05050005025010051")
        self.assertEqual(unmapped, [])
        self.assertEqual(options, {"ravagerRank": 2})
        self.assertEqual(option_mappings[0]["semantic"], "ravager")
        self.assertEqual(option_mappings[0]["rank"], 2)
        self.assertEqual(option_mappings[0]["status"], "applied_outside_talents_string")
        # Fury character 12 is Improved Slam in the old proto and must be zero.
        self.assertEqual(trees["fury"][11], "0")

        _, unknown_unmapped, unknown_trees, unknown_options, _ = warrior_talents_to_string(
            [{
                "tab": 2,
                "index": 11,
                "tier": 5,
                "column": 1,
                "rank": 3,
                "maxRank": 3,
                "name": "localized-name-not-known-here",
            }]
        )
        self.assertEqual(unknown_unmapped, [])
        self.assertEqual(unknown_options["ravagerRank"], 3)
        self.assertEqual(unknown_trees["fury"], "")

        moved_slam_string, moved_unmapped, moved_trees, _, _ = warrior_talents_to_string(
            [{"tab": 1, "rank": 5, "name": "强化猛击"}]
        )
        self.assertEqual(moved_unmapped, [])
        self.assertEqual(moved_trees["fury"], "000000000005")
        self.assertEqual(moved_slam_string, "-000000000005")

    def test_profile_replaces_only_observed_player_static_fields(self) -> None:
        template = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
        original = deepcopy(template)
        selection = ProfileSelection(
            path=Path("calibration.jsonl"),
            line_number=4,
            record={"event": "STATIC_PROFILE_CAPTURED", "sequence": 4, "state": _state()},
            selection_mode="latest_static_profile",
        )

        request, metadata = build_wowsims_profile(template, selection)
        player = request["raid"]["parties"][0]["players"][0]

        self.assertEqual(player["name"], "TestWarrior")
        self.assertEqual(player["race"], "RaceGnome")
        self.assertEqual(player["class"], "ClassWarrior")
        self.assertEqual(player["talentsString"], "30205020332-05050005025010051")
        self.assertEqual(player["warrior"]["options"]["ravagerRank"], 2)
        self.assertEqual(len(player["equipment"]["items"]), 17)
        self.assertEqual(player["equipment"]["items"][0], {"id": 21329})
        self.assertEqual(player["equipment"]["items"][2], {"id": 21330, "enchant": 3038})
        self.assertEqual(player["equipment"]["items"][14], {"id": 21679})
        self.assertEqual(player["equipment"]["items"][15], {})
        self.assertEqual(request["encounter"], original["encounter"])
        self.assertEqual(request["raid"]["buffs"], original["raid"]["buffs"])
        self.assertEqual(request["raid"]["parties"][0]["buffs"], original["raid"]["parties"][0]["buffs"])
        original_options = original["raid"]["parties"][0]["players"][0]["warrior"]["options"]
        self.assertEqual(
            player["warrior"]["options"]["startingRage"],
            original_options["startingRage"],
        )
        self.assertEqual(
            player["warrior"]["options"]["stance"],
            original_options["stance"],
        )
        self.assertEqual(player["rotation"], original["raid"]["parties"][0]["players"][0]["rotation"])
        self.assertEqual(metadata["observed_character"]["level"], 60)
        self.assertEqual(metadata["observed_character"]["static_counts"]["equipment"], 3)
        self.assertEqual(metadata["observed_character"]["static_counts"]["talents"], 16)
        self.assertEqual(metadata["observed_character"]["static_counts"]["spellbook"], 1)
        self.assertEqual(metadata["unmapped_talents"], [])
        self.assertEqual(metadata["talent_option_mappings"][0]["semantic"], "ravager")
        self.assertEqual(metadata["talent_option_mappings"][0]["status"], "applied_outside_talents_string")
        self.assertEqual(metadata["simulator_semantic_defaults"][0]["value"], 0)

    def test_metadata_reports_observed_improved_slam_semantic_rank(self) -> None:
        template = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
        state = _state()
        state["talents"] = [{"tab": 1, "rank": 5, "name": "强化猛击"}]
        selection = ProfileSelection(
            path=Path("calibration.jsonl"),
            line_number=1,
            record={"event": "STATIC_PROFILE_CAPTURED", "sequence": 1, "state": state},
            selection_mode="latest_static_profile",
        )

        request, metadata = build_wowsims_profile(template, selection)

        player = request["raid"]["parties"][0]["players"][0]
        self.assertEqual(player["talentsString"], "-000000000005")
        self.assertEqual(player["warrior"]["options"]["ravagerRank"], 0)
        self.assertEqual(metadata["simulator_semantic_defaults"][0]["value"], 5)

    def test_learned_unmapped_talent_fails_instead_of_publishing_partial_profile(self) -> None:
        template = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
        state = _state()
        state["talents"] = [
            {"tab": 2, "index": 99, "rank": 1, "name": "尚未建模的乌龟天赋"}
        ]
        selection = ProfileSelection(
            path=Path("calibration.jsonl"),
            line_number=1,
            record={"event": "STATIC_PROFILE_CAPTURED", "sequence": 1, "state": state},
            selection_mode="latest_static_profile",
        )

        with self.assertRaisesRegex(
            WowsimsProfileError,
            "learned Turtle Warrior talent has no simulator mapping.*尚未建模的乌龟天赋",
        ):
            build_wowsims_profile(template, selection)

    def test_latest_static_profile_wins_and_old_data_falls_back_to_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "capture.jsonl"
            rows = [
                {"event": "CALIBRATION_TASK_STARTED", "sequence": 1, "state": _state(111, static=False)},
                {"event": "STATIC_PROFILE_CAPTURED", "sequence": 2, "state": _state(222)},
                {"event": "CALIBRATION_TASK_COMPLETED", "sequence": 3, "state": _state(333, static=False)},
                {
                    "event": "STATIC_PROFILE_CAPTURED",
                    "sequence": 4,
                    "state": {**_state(444), "talents": [], "skillLines": []},
                },
            ]
            source.write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                encoding="utf-8",
            )
            selected = select_profile_record([source])
            self.assertEqual(selected.selection_mode, "latest_static_profile")
            self.assertIn("item:222:", selected.record["state"]["equipment"][2]["link"])

            old_source = root / "old.jsonl"
            old_source.write_text(
                json.dumps(rows[0], ensure_ascii=False) + "\n" +
                json.dumps({"event": "UNIT_RAGE", "sequence": 4, "state": {"rage": 10}}) + "\n",
                encoding="utf-8",
            )
            selected_old = select_profile_record([old_source])
            self.assertEqual(selected_old.selection_mode, "latest_boundary_fallback")
            template = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
            with self.assertRaisesRegex(
                WowsimsProfileError,
                "live profile race was not observed",
            ):
                build_wowsims_profile(template, selected_old)

            output = root / "must-not-exist.json"
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                return_code = main(
                    [
                        str(old_source),
                        "--template",
                        str(TEMPLATE_PATH),
                        "--output",
                        str(output),
                    ]
                )
            self.assertEqual(return_code, 2)
            self.assertIn("no STATIC_PROFILE_CAPTURED record was found", stderr.getvalue())
            self.assertFalse(output.exists())

    def test_item_link_parser_preserves_item_enchant_and_random_suffix(self) -> None:
        self.assertEqual(
            parse_item_link("|cff0070dd|Hitem:12345:1900:-7:99|h[item]|h|r"),
            {"id": 12345, "enchant": 1900, "randomSuffix": -7},
        )
        with self.assertRaises(WowsimsProfileError):
            parse_item_link("not an item link")

    def test_windows_cli_writes_request_and_separate_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "capture.jsonl"
            output = root / "live.json"
            source.write_text(
                json.dumps(
                    {"event": "STATIC_PROFILE_CAPTURED", "sequence": 1, "state": _state()},
                    ensure_ascii=False,
                ) + "\n",
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                return_code = main(
                    [str(source), "--template", str(TEMPLATE_PATH), "--output", str(output)]
                )

            self.assertEqual(return_code, 0)
            receipt = json.loads(stdout.getvalue())
            self.assertEqual(receipt["source_event"], "STATIC_PROFILE_CAPTURED")
            self.assertTrue(output.is_file())
            self.assertTrue((root / "live.metadata.json").is_file())
            request = json.loads(output.read_text(encoding="utf-8"))
            self.assertNotIn("metadata", request)


if __name__ == "__main__":
    unittest.main()
