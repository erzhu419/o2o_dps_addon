from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps.fury_expert_runtime_snapshot_v1 import (
    CAT_PROFILE_KEYS,
    FuryExpertRuntimeSnapshotError,
    SCHEMA,
    capture_runtime_snapshot,
    sha256_json,
    write_content_addressed_snapshot,
)


def _lua_value(value: object) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _lua_table(values: dict[str, object]) -> str:
    return "{" + ",".join(
        f"[{json.dumps(key, ensure_ascii=False)}]={_lua_value(value)}"
        for key, value in sorted(values.items())
    ) + ",}"


def _cat_source() -> str:
    profile: dict[str, object] = {key: 0 for key in CAT_PROFILE_KEYS}
    profile.update(
        {
            "BattleShout": 1,
            "BerserkerRage": 1,
            "BerserkerStance": 1,
            "ExecuteWithoutMonster": 1,
            "Hamstring": 1,
            "HeroicStrike_Value": 50,
            "NearbyEnemies": 1,
            "NearbyEnemies_Value": 8,
            "Slam_Value": 1.5,
            "UseExecute": 1,
            "Whirlwind": 1,
        }
    )
    body = _lua_table(profile)
    return f"MPWarriorFurySaved={{[1]={body},[2]={body},[3]={body},[\"Version\"]=32,}}\n"


def _contra_source(*, xuanfeng: bool = False) -> str:
    buttons = {
        "fangan": "方案1",
        "mode": "副本模式",
        "xuanfeng": xuanfeng,
        "silie": True,
        "shengcun": True,
        "baofa": True,
        "autoselect": False,
        "quanbudaduan": False,
        "zhidingdaduan": False,
        "liunudaduan": False,
        "bossothuanwuqi": False,
        "xiaoguaiothuanwuqi": False,
    }
    scheme = dict(buttons)
    scheme.pop("fangan")
    return (
        "ContraDB={"
        f"[\"Warrior\"]={{[\"Buttons\"]={_lua_table(buttons)},"
        f"[\"fangan1\"]={_lua_table(scheme)},}},"
        "[\"BehindOrFace\"]={[\"Settings\"]={[\"Settings\"]=nil "
        "--[[ skipped recursive table ]],[\"Enabled\"]=false,},},}\n"
    )


def _write(path: Path, value: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value if isinstance(value, bytes) else value.encode("utf-8"))


class FuryExpertRuntimeSnapshotV1Tests(unittest.TestCase):
    def _inputs(self, root: Path) -> dict[str, Path]:
        savedvariables = (
            root / "Account" / "RealmOne" / "CharacterOne" / "SavedVariables"
        )
        paths = {
            "cat_savedvariables": savedvariables / "Cat.lua",
            "contra_savedvariables": savedvariables / "Contra.lua",
            "wowsims_profile": root / "profile.json",
            "wowsims_metadata": root / "metadata.json",
            "config_wtf": root / "Config.wtf",
            "nampower_dll": root / "nampower.dll",
            "superwow_dll": root / "SuperWoWhook.dll",
        }
        _write(paths["cat_savedvariables"], _cat_source())
        _write(paths["contra_savedvariables"], _contra_source())
        equipment = [{} for _ in range(17)]
        equipment[0] = {"id": 1}
        equipment[14] = {"id": 2}
        player = {
            "name": "FuryResearchCharacter",
            "race": "RaceGnome",
            "class": "ClassWarrior",
            "talentsString": "3",
            "equipment": {"items": equipment},
            "warrior": {"options": {"ravagerRank": 0}},
        }
        profile = {"raid": {"parties": [{"players": [player]}]}}
        metadata = {
            "mapped": {
                "name": player["name"],
                "race": player["race"],
                "class": player["class"],
                "talents_string": player["talentsString"],
                "equipment_slots": 17,
                "talent_trees": {"arms": "3", "fury": "", "protection": ""},
            },
            "source": {
                "calibration_jsonl": str(root / "calibration.jsonl"),
                "line_number": 1,
                "event": "STATIC_PROFILE_CAPTURED",
                "sequence": 7,
            },
            "observed_character": {
                "identity": {
                    "name": player["name"],
                    "classFile": "WARRIOR",
                    "raceFile": "Gnome",
                    "level": 60,
                },
                "static_counts": {"equipment": 2, "talents": 1},
            },
            "talent_option_mappings": [],
        }
        capture = {
            "event": "STATIC_PROFILE_CAPTURED",
            "sequence": 7,
            "state": {
                "playerGUID": "0x00000000000000A1",
                "characterIdentity": {
                    "name": "CharacterOne",
                    "classFile": "WARRIOR",
                    "raceFile": "Gnome",
                    "level": 60,
                },
                "equipment": [
                    {"slot": 1, "link": "|Hitem:1:0:0:0|h[Test Head]|h"},
                    {"slot": 16, "link": "|Hitem:2:0:0:0|h[Test Weapon]|h"},
                ],
                "talents": [
                    {
                        "tab": 1,
                        "index": 1,
                        "tier": 1,
                        "column": 1,
                        "name": "Improved Heroic Strike",
                        "rank": 3,
                        "maxRank": 3,
                    }
                ],
            },
        }
        _write(root / "calibration.jsonl", json.dumps(capture) + "\n")
        _write(paths["wowsims_profile"], json.dumps(profile))
        _write(paths["wowsims_metadata"], json.dumps(metadata))
        _write(
            paths["config_wtf"],
            'SET NP_QueueSpellsOnCooldown "0"\nSET NP_QueueOnSwingSpells "1"\n',
        )
        _write(paths["nampower_dll"], b"nampower-v4.1.0")
        _write(paths["superwow_dll"], b"superwow")
        return paths

    def test_snapshot_binds_profiles_build_extensions_and_stays_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            inputs = self._inputs(Path(directory))
            result = capture_runtime_snapshot(**inputs)

        self.assertEqual(result["schema"], SCHEMA)
        self.assertEqual(result["cat_profile1"]["profile_slot"], 1)
        self.assertEqual(
            result["cat_profile1"]["adapter_core_projection"]["slam_timing_s"], 1.5
        )
        self.assertEqual(result["contra_current_profile"]["selected_scheme_index"], 1)
        self.assertFalse(
            result["contra_current_profile"]["adapter_core_projection"]["xuanfeng"]
        )
        self.assertEqual(result["fixed_character_build"]["equipment_slot_count"], 17)
        self.assertEqual(
            result["character_context"]["status"],
            "BOUND_SAME_CHARACTER_DIRECTORY_AND_BUILD_CAPTURE",
        )
        self.assertEqual(
            result["character_context"]["character_directory"], "CharacterOne"
        )
        self.assertEqual(result["nampower_cvars"]["NP_QueueSpellsOnCooldown"], "0")
        self.assertFalse(result["authority"]["comparison_eligible"])
        self.assertTrue(result["remaining_blockers"])
        without_hash = deepcopy(result)
        digest = without_hash.pop("snapshot_sha256")
        self.assertEqual(digest, sha256_json(without_hash))

    def test_content_addressed_writer_is_idempotent_and_tamper_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = capture_runtime_snapshot(**self._inputs(root / "inputs"))
            first = write_content_addressed_snapshot(result, root / "output")
            second = write_content_addressed_snapshot(result, root / "output")
            self.assertEqual(first, second)
            self.assertEqual(
                hashlib.sha256(first.read_bytes()).hexdigest(),
                hashlib.sha256(second.read_bytes()).hexdigest(),
            )
            changed = deepcopy(result)
            changed["cat_profile1"]["profile_slot"] = 2
            with self.assertRaisesRegex(FuryExpertRuntimeSnapshotError, "snapshot_sha256"):
                write_content_addressed_snapshot(changed, root / "output")

    def test_cat_profile_key_drift_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = self._inputs(root)
            text = inputs["cat_savedvariables"].read_text(encoding="utf-8")
            text = text.replace('["Whirlwind"]=1,', "")
            inputs["cat_savedvariables"].write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(FuryExpertRuntimeSnapshotError, "keys drifted"):
                capture_runtime_snapshot(**inputs)

    def test_contra_selected_scheme_and_comment_shape_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = self._inputs(root)
            text = inputs["contra_savedvariables"].read_text(encoding="utf-8")
            text = text.replace('"方案1"', '"无方案"', 1)
            inputs["contra_savedvariables"].write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(FuryExpertRuntimeSnapshotError, "numeric suffix"):
                capture_runtime_snapshot(**inputs)

    def test_profile_metadata_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = self._inputs(root)
            metadata = json.loads(inputs["wowsims_metadata"].read_text())
            metadata["mapped"]["race"] = "RaceOrc"
            inputs["wowsims_metadata"].write_text(json.dumps(metadata))
            with self.assertRaisesRegex(FuryExpertRuntimeSnapshotError, "differs"):
                capture_runtime_snapshot(**inputs)

    def test_cross_character_savedvariables_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = self._inputs(root)
            other = root / "Account" / "RealmOne" / "Other" / "SavedVariables" / "Contra.lua"
            _write(other, _contra_source())
            inputs["contra_savedvariables"] = other
            with self.assertRaisesRegex(
                FuryExpertRuntimeSnapshotError, "same character directory"
            ):
                capture_runtime_snapshot(**inputs)

    def test_build_capture_character_must_match_savedvariables_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = self._inputs(root)
            capture_path = root / "calibration.jsonl"
            capture = json.loads(capture_path.read_text(encoding="utf-8"))
            capture["state"]["characterIdentity"]["name"] = "Other"
            capture_path.write_text(json.dumps(capture) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(
                FuryExpertRuntimeSnapshotError,
                "build capture character does not match",
            ):
                capture_runtime_snapshot(**inputs)

    def test_same_counts_but_different_profile_equipment_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = self._inputs(root)
            profile = json.loads(inputs["wowsims_profile"].read_text(encoding="utf-8"))
            profile["raid"]["parties"][0]["players"][0]["equipment"]["items"][0] = {
                "id": 999
            }
            inputs["wowsims_profile"].write_text(json.dumps(profile), encoding="utf-8")
            with self.assertRaisesRegex(
                FuryExpertRuntimeSnapshotError,
                "equipment differs from the exact static capture projection",
            ):
                capture_runtime_snapshot(**inputs)


if __name__ == "__main__":
    unittest.main()
