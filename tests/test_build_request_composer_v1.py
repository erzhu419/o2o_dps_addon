from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.build_request_composer_v1 import (
    BuildRequestComposerV1Error,
    CharacterProfile,
    EncounterModel,
    ExecutionModel,
    MISSING,
    Objective,
    RaidContext,
    compose_build_request_v1,
)
from o2o_dps.wowsims_profile import ProfileSelection


def _talents(*, ravager_rank: int = 2, improved_slam_rank: int = 0):
    rows = [
        {
            "tab": 2,
            "index": 11,
            "tier": 5,
            "column": 1,
            "rank": ravager_rank,
            "maxRank": 3,
            "name": "碾碎",
        }
    ]
    if improved_slam_rank:
        rows.append(
            {
                "tab": 2,
                "index": 98,
                "rank": improved_slam_rank,
                "maxRank": 5,
                "name": "强化猛击",
            }
        )
    return rows


def _state(
    *,
    name: str = "CurrentWarrior",
    race: str = "Gnome",
    head_id: int = 21329,
    weapon_id: int = 21679,
    ravager_rank: int = 2,
    improved_slam_rank: int = 0,
):
    return {
        "characterIdentity": {
            "name": name,
            "level": 60,
            "raceFile": race,
            "classFile": "WARRIOR",
        },
        "equipment": [
            {
                "slot": 1,
                "link": f"|cffa335ee|Hitem:{head_id}:0:0:0|h[Head]|h|r",
                "itemInfo": {"equipLoc": "INVTYPE_HEAD"},
            },
            {
                "slot": 16,
                "link": f"|cffa335ee|Hitem:{weapon_id}:1900:0:0|h[Weapon]|h|r",
                "itemInfo": {"equipLoc": "INVTYPE_2HWEAPON"},
            },
        ],
        "talents": _talents(
            ravager_rank=ravager_rank,
            improved_slam_rank=improved_slam_rank,
        ),
        "spellbook": [{"name": "嗜血", "rank": "等级 4"}],
        "actionBarSpells": [{"slot": 1, "spellID": 23894}],
        "skillLines": [{"name": "双手剑", "rank": 305}],
        "bagItems": [],
    }


def _selection(
    *,
    name: str = "CurrentWarrior",
    race: str = "Gnome",
    head_id: int = 21329,
    weapon_id: int = 21679,
    ravager_rank: int = 2,
    improved_slam_rank: int = 0,
    full: bool = True,
):
    event = "STATIC_PROFILE_CAPTURED" if full else "CALIBRATION_TASK_COMPLETED"
    return ProfileSelection(
        path=Path(f"{name}.jsonl"),
        line_number=7,
        record={
            "event": event,
            "sequence": 7,
            "provenance": {"import_id": f"import-{name}"},
            "state": _state(
                name=name,
                race=race,
                head_id=head_id,
                weapon_id=weapon_id,
                ravager_rank=ravager_rank,
                improved_slam_rank=improved_slam_rank,
            ),
        },
        selection_mode="latest_static_profile" if full else "latest_boundary_fallback",
    )


def _coverage(item_id: int, *, enchant: bool = False, effect="NO_SPECIAL_EFFECT"):
    return {
        "item_id": item_id,
        "item_definition_status": "KNOWN",
        "item_effect_status": effect,
        "permanent_enchant_status": "IMPLEMENTED" if enchant else "NONE",
        "temporary_enchant_status": "NONE",
        "temporary_enchant_id": None,
        "provenance": {"source": "fixture-mechanics-registry-v1"},
    }


def _character(
    selection: ProfileSelection | None = None,
    *,
    consumes=None,
    slot_statuses=None,
    coverage=None,
):
    selected = selection or _selection()
    state = selected.record["state"]
    equipment = {row["slot"]: row for row in state["equipment"]}
    head_id = int(equipment[1]["link"].split("item:", 1)[1].split(":", 1)[0])
    weapon_id = int(equipment[16]["link"].split("item:", 1)[1].split(":", 1)[0])
    return CharacterProfile(
        selection=selected,
        consumes=consumes if consumes is not None else {"defaultPotion": "MightyRagePotion"},
        database={},
        slot_statuses=slot_statuses if slot_statuses is not None else {},
        item_effect_coverage=(
            coverage
            if coverage is not None
            else {
                1: _coverage(head_id),
                16: _coverage(weapon_id, enchant=True, effect="IMPLEMENTED"),
            }
        ),
        provenance={"source": "live-static-profile"},
    )


def _raid_context(*, marker: str = "current"):
    return RaidContext(
        individual_buffs={"blessingOfKings": True, "marker": marker},
        party_buffs={"battleShout": "TristateEffectImproved"},
        raid_buffs={"arcaneBrilliance": True},
        debuffs={"sunderArmor": True},
        additional_party_players=[],
        additional_parties=[],
        raid_options={"numActiveParties": 1},
        provenance={"source": f"raid-context-{marker}"},
    )


def _encounter(*, duration: float = 20.0):
    return EncounterModel(
        request_fields={
            "duration": duration,
            "targets": [
                {
                    "name": "Target Dummy",
                    "level": 63,
                    "stats": [0] * 46,
                    "tankIndex": -1,
                }
            ],
        },
        provenance={"source": "fixture-encounter-v1"},
    )


def _execution(*, starting_rage: int = 40, marker: str = "current"):
    return ExecutionModel(
        rotation={"type": marker},
        cooldowns={"cooldowns": []},
        warrior_options={
            "startingRage": starting_rage,
            "stance": "WarriorStanceBerserker",
            "queueDelay": 25,
        },
        reaction_time_ms=125,
        channel_clip_delay_ms=0,
        in_front_of_target=False,
        distance_from_target=3,
        provenance={"source": f"execution-{marker}"},
    )


def _objective():
    return Objective(
        kind="TARGET_DUMMY_DPS",
        sim_options={"iterations": 1, "randomSeed": "20260912", "interactive": True},
        provenance={"source": "paired-seed-protocol-v1"},
    )


class BuildRequestComposerV1Tests(unittest.TestCase):
    def test_five_components_compose_without_legacy_template_residual(self):
        result = compose_build_request_v1(
            _character(), _raid_context(), _encounter(), _execution(), _objective()
        )

        self.assertTrue(result.admitted)
        request = result.request
        assert request is not None
        player = request["raid"]["parties"][0]["players"][0]
        self.assertEqual(set(request), {"raid", "encounter", "simOptions"})
        self.assertEqual(player["name"], "CurrentWarrior")
        self.assertEqual(player["race"], "RaceGnome")
        self.assertEqual(player["equipment"]["items"][14]["id"], 21679)
        self.assertEqual(player["consumes"], {"defaultPotion": "MightyRagePotion"})
        self.assertEqual(player["buffs"]["marker"], "current")
        self.assertEqual(player["rotation"], {"type": "current"})
        self.assertEqual(player["warrior"]["options"]["startingRage"], 40)
        self.assertEqual(request["raid"]["buffs"], {"arcaneBrilliance": True})
        self.assertEqual(request["raid"]["debuffs"], {"sunderArmor": True})
        self.assertEqual(request["encounter"]["duration"], 20.0)
        self.assertEqual(request["simOptions"]["randomSeed"], "20260912")

        audit = result.audit
        self.assertEqual(audit["admission"]["status"], "ADMITTED")
        self.assertEqual(audit["request_validation"]["status"], "PASS")
        self.assertEqual(audit["residual_audit"]["status"], "PASS")
        self.assertFalse(audit["residual_audit"]["template_used"])
        self.assertEqual(audit["residual_audit"]["legacy_template_fields_inherited"], [])
        self.assertEqual(audit["residual_audit"]["unattributed_paths"], [])
        self.assertEqual(
            audit["provenance"]["character_profile"]["selection"]["path"],
            "CurrentWarrior.jsonl",
        )
        self.assertEqual(audit["provenance"]["raid_context"]["source"], "raid-context-current")
        self.assertEqual(audit["objective_kind"], "TARGET_DUMMY_DPS")

    def test_second_character_cannot_inherit_first_character_fields(self):
        first = compose_build_request_v1(
            _character(consumes={"flask": "old"}),
            _raid_context(marker="old"),
            _encounter(),
            _execution(starting_rage=100, marker="old"),
            _objective(),
        )
        frozen_first = deepcopy(first.as_dict())

        second_selection = _selection(
            name="NewWarrior",
            race="Orc",
            head_id=12640,
            weapon_id=17076,
            ravager_rank=0,
            improved_slam_rank=5,
        )
        second = compose_build_request_v1(
            _character(
                second_selection,
                consumes={},
                coverage={
                    1: _coverage(12640),
                    16: _coverage(17076, enchant=True, effect="IMPLEMENTED"),
                },
            ),
            RaidContext(
                individual_buffs={},
                party_buffs={},
                raid_buffs={},
                debuffs={},
                additional_party_players=[],
                additional_parties=[],
                raid_options={},
                provenance={"source": "empty-new-context"},
            ),
            _encounter(),
            _execution(starting_rage=0, marker="new"),
            _objective(),
        )

        self.assertEqual(first.as_dict(), frozen_first)
        self.assertTrue(second.admitted)
        rendered = json.dumps(second.as_dict(), ensure_ascii=False)
        self.assertNotIn("CurrentWarrior", rendered)
        self.assertNotIn('"old"', rendered)
        player = second.request["raid"]["parties"][0]["players"][0]
        self.assertEqual(player["name"], "NewWarrior")
        self.assertEqual(player["race"], "RaceOrc")
        self.assertEqual(player["consumes"], {})
        self.assertEqual(player["buffs"], {})
        self.assertEqual(player["rotation"], {"type": "new"})
        self.assertEqual(player["warrior"]["options"]["startingRage"], 0)

    def test_observed_empty_offhand_and_missing_offhand_are_distinct(self):
        observed_empty = compose_build_request_v1(
            _character(), _raid_context(), _encounter(), _execution(), _objective()
        )
        empty_equipment = observed_empty.audit["coverage"]["equipment"]
        offhand = next(row for row in empty_equipment["slots"] if row["wow_slot"] == 17)
        self.assertEqual(offhand["status"], "OBSERVED_EMPTY")
        self.assertEqual(
            empty_equipment["offhand_semantics"], "LEGAL_EMPTY_TWO_HAND_MAIN_HAND"
        )
        self.assertTrue(observed_empty.admitted)

        missing = compose_build_request_v1(
            _character(slot_statuses={17: MISSING}),
            _raid_context(),
            _encounter(),
            _execution(),
            _objective(),
        )
        missing_equipment = missing.audit["coverage"]["equipment"]
        missing_offhand = next(
            row for row in missing_equipment["slots"] if row["wow_slot"] == 17
        )
        self.assertEqual(missing_offhand["status"], "MISSING")
        self.assertEqual(missing_equipment["offhand_semantics"], "MISSING_NOT_EMPTY")
        self.assertIsNone(missing.request)
        self.assertEqual(missing.audit["admission"]["status"], "BLOCKED")
        self.assertIn(
            {"code": "EQUIPMENT_SLOT_MISSING", "wow_slot": 17},
            missing.audit["admission"]["blockers"],
        )

        partial = compose_build_request_v1(
            _character(_selection(full=False)),
            _raid_context(),
            _encounter(),
            _execution(),
            _objective(),
        )
        partial_equipment = partial.audit["coverage"]["equipment"]
        self.assertEqual(partial_equipment["capture_scope"], "PARTIAL_OR_BOUNDARY_CAPTURE")
        self.assertEqual(partial_equipment["offhand_semantics"], "MISSING_NOT_EMPTY")
        self.assertIsNone(partial.request)

    def test_ravager_and_improved_slam_have_separate_owned_targets(self):
        selected = _selection(ravager_rank=2, improved_slam_rank=5)
        result = compose_build_request_v1(
            _character(selected), _raid_context(), _encounter(), _execution(), _objective()
        )

        talents = result.audit["coverage"]["talents"]
        self.assertEqual(talents["semantic_separation"], "PASS")
        self.assertEqual(talents["ravager"], {"target": "warrior.options.ravagerRank", "rank": 2})
        self.assertEqual(
            talents["improved_slam"],
            {"target": "WarriorTalents.improvedSlam", "rank": 5},
        )
        player = result.request["raid"]["parties"][0]["players"][0]
        self.assertEqual(player["warrior"]["options"]["ravagerRank"], 2)
        fury_tree = player["talentsString"].split("-")[1]
        self.assertEqual(fury_tree[11], "5")

        with self.assertRaisesRegex(
            BuildRequestComposerV1Error, "must not own character-derived ravagerRank"
        ):
            compose_build_request_v1(
                _character(selected),
                _raid_context(),
                _encounter(),
                ExecutionModel(
                    rotation={},
                    cooldowns={},
                    warrior_options={"ravagerRank": 2},
                    reaction_time_ms=0,
                    channel_clip_delay_ms=0,
                    in_front_of_target=False,
                    distance_from_target=3,
                    provenance={"source": "bad-owner"},
                ),
                _objective(),
            )

    def test_unknown_item_effect_is_reported_and_request_is_withheld(self):
        unknown = compose_build_request_v1(
            _character(
                coverage={
                    1: _coverage(21329),
                    16: _coverage(21679, enchant=True, effect="UNKNOWN"),
                }
            ),
            _raid_context(),
            _encounter(),
            _execution(),
            _objective(),
        )

        self.assertFalse(unknown.admitted)
        self.assertIsNone(unknown.request)
        weapon = next(
            row
            for row in unknown.audit["coverage"]["equipment"]["item_effects"]
            if row["wow_slot"] == 16
        )
        self.assertEqual(weapon["item_effect_status"], "UNKNOWN")
        self.assertFalse(weapon["runtime_executable"])
        codes = {row["code"] for row in unknown.audit["admission"]["blockers"]}
        self.assertIn("ITEM_EFFECT_NOT_EXECUTABLE", codes)

        undeclared = compose_build_request_v1(
            _character(coverage={1: _coverage(21329)}),
            _raid_context(),
            _encounter(),
            _execution(),
            _objective(),
        )
        self.assertIsNone(undeclared.request)
        self.assertIn(
            "ITEM_EFFECT_COVERAGE_MISSING",
            {row["code"] for row in undeclared.audit["admission"]["blockers"]},
        )

    def test_implemented_temporary_enchant_is_not_silently_omitted_or_doubled(self):
        weapon = _coverage(21679, enchant=True, effect="IMPLEMENTED")
        weapon["temporary_enchant_status"] = "IMPLEMENTED"
        weapon["temporary_enchant_id"] = 2504
        result = compose_build_request_v1(
            _character(
                consumes={"denseSharpeningStone": True},
                coverage={1: _coverage(21329), 16: weapon},
            ),
            _raid_context(),
            _encounter(),
            _execution(),
            _objective(),
        )

        self.assertFalse(result.admitted)
        self.assertIsNone(result.request)
        codes = {row["code"] for row in result.audit["admission"]["blockers"]}
        self.assertIn("TEMPORARY_ENCHANT_NOT_ENCODED_IN_REQUEST", codes)
        row = next(
            item
            for item in result.audit["coverage"]["equipment"]["item_effects"]
            if item["wow_slot"] == 16
        )
        self.assertEqual(row["temporary_enchant_request_representation"], "NOT_ENCODED")
        self.assertFalse(row["runtime_executable"])

    def test_existing_full_policy_request_validation_remains_the_gate(self):
        with self.assertRaisesRegex(
            BuildRequestComposerV1Error,
            "existing full-policy request validation failed.*duration must be positive",
        ):
            compose_build_request_v1(
                _character(),
                _raid_context(),
                _encounter(duration=0),
                _execution(),
                _objective(),
            )

    def test_unknown_talent_is_not_silently_dropped(self):
        selected = _selection()
        selected.record["state"]["talents"] = [
            {"tab": 2, "index": 99, "rank": 1, "name": "未知乌龟天赋"}
        ]
        with self.assertRaisesRegex(
            BuildRequestComposerV1Error,
            "learned Turtle Warrior talent has no simulator mapping",
        ):
            compose_build_request_v1(
                _character(selected),
                _raid_context(),
                _encounter(),
                _execution(),
                _objective(),
            )


if __name__ == "__main__":
    unittest.main()
