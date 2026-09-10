"""Build a wowsims-turtle RaidSimRequest from an in-game static profile.

The source is an imported BrainOfCat calibration JSONL file.  Generating a
live request requires a full ``STATIC_PROFILE_CAPTURED`` record so an older
partial boundary cannot silently inherit the template character's identity.
The supplied simulator template remains the authority for encounter, buffs,
rotation, consumes, and Warrior options.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Iterable, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CALIBRATION_ROOT = PROJECT_ROOT / "offline_data" / "calibration"
DEFAULT_TEMPLATE = PROJECT_ROOT / "configs" / "wowsims" / "fury_warrior_phase1.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "configs" / "wowsims" / "fury_warrior_live.json"

WOW_SLOT_TO_WOWSIMS_SLOT = {
    1: 0,   # head
    2: 1,   # neck
    3: 2,   # shoulder
    15: 3,  # back
    5: 4,   # chest
    9: 5,   # wrist
    10: 6,  # hands
    6: 7,   # waist
    7: 8,   # legs
    8: 9,   # feet
    11: 10, # finger 1
    12: 11, # finger 2
    13: 12, # trinket 1
    14: 13, # trinket 2
    16: 14, # main hand
    17: 15, # off hand
    18: 16, # ranged
}
WOWSIMS_EQUIPMENT_SLOT_COUNT = 17

RACE_ENUMS = {
    "DWARF": "RaceDwarf",
    "GNOME": "RaceGnome",
    "HUMAN": "RaceHuman",
    "NIGHTELF": "RaceNightElf",
    "ORC": "RaceOrc",
    "TAUREN": "RaceTauren",
    "TROLL": "RaceTroll",
    "UNDEAD": "RaceUndead",
    "SCOURGE": "RaceUndead",
}
CLASS_ENUMS = {
    "DRUID": "ClassDruid",
    "HUNTER": "ClassHunter",
    "MAGE": "ClassMage",
    "PALADIN": "ClassPaladin",
    "PRIEST": "ClassPriest",
    "ROGUE": "ClassRogue",
    "SHAMAN": "ClassShaman",
    "WARLOCK": "ClassWarlock",
    "WARRIOR": "ClassWarrior",
}

# Field order is the exact order consumed by core.FillTalentsProto for the
# current Warrior proto. It must not be confused with Turtle's GetTalentInfo
# indices: Turtle moved several talents, and Ravager (Chinese client: 碾碎)
# is a separate semantic from Improved Slam.
WARRIOR_TALENT_FIELDS = (
    (
        "improvedHeroicStrike",
        "deflection",
        "improvedRend",
        "improvedCharge",
        "tacticalMastery",
        "improvedThunderClap",
        "improvedOverpower",
        "angerManagement",
        "deepWounds",
        "twoHandedWeaponSpecialization",
        "impale",
        "axeSpecialization",
        "sweepingStrikes",
        "maceSpecialization",
        "swordSpecialization",
        "polearmSpecialization",
        "improvedHamstring",
        "mortalStrike",
    ),
    (
        "boomingVoice",
        "cruelty",
        "improvedDemoralizingShout",
        "unbridledWrath",
        "improvedCleave",
        "piercingHowl",
        "bloodCraze",
        "improvedBattleShout",
        "dualWieldSpecialization",
        "improvedExecute",
        "enrage",
        "improvedSlam",
        "deathWish",
        "improvedIntercept",
        "improvedBerserkerRage",
        "flurry",
        "bloodthirst",
    ),
    (
        "shieldSpecialization",
        "anticipation",
        "improvedBloodrage",
        "toughness",
        "ironWill",
        "lastStand",
        "improvedShieldBlock",
        "improvedRevenge",
        "defiance",
        "improvedSunderArmor",
        "improvedDisarm",
        "improvedTaunt",
        "improvedShieldWall",
        "concussionBlow",
        "improvedShieldBash",
        "oneHandedWeaponSpecialization",
        "shieldSlam",
    ),
)

_DISPLAY_ALIASES = {
    # Arms.
    "improvedHeroicStrike": ("Improved Heroic Strike", "强化英勇打击"),
    "deflection": ("Deflection", "偏斜"),
    "improvedRend": ("Improved Rend", "强化撕裂"),
    "improvedCharge": ("Improved Charge", "强化冲锋"),
    "tacticalMastery": ("Tactical Mastery", "战术掌握"),
    "improvedThunderClap": ("Improved Thunder Clap", "强化雷霆一击"),
    "improvedOverpower": ("Improved Overpower", "强化压制"),
    "angerManagement": ("Anger Management", "愤怒掌控"),
    "deepWounds": ("Deep Wounds", "重伤"),
    "twoHandedWeaponSpecialization": (
        "Two-Handed Weapon Specialization",
        "Two Handed Weapon Specialization",
        "双手武器专精",
    ),
    "impale": ("Impale", "穿刺"),
    "axeSpecialization": ("Axe Specialization", "斧专精", "斧类武器专精"),
    "sweepingStrikes": ("Sweeping Strikes", "横扫攻击"),
    "maceSpecialization": ("Mace Specialization", "锤专精", "锤类武器专精"),
    "swordSpecialization": ("Sword Specialization", "剑专精", "剑类武器专精"),
    "polearmSpecialization": ("Polearm Specialization", "长柄武器专精"),
    "improvedHamstring": ("Improved Hamstring", "强化断筋"),
    "mortalStrike": ("Mortal Strike", "致死打击"),
    # Fury.
    "boomingVoice": ("Booming Voice", "震耳嗓音"),
    "cruelty": ("Cruelty", "残忍"),
    "improvedDemoralizingShout": (
        "Improved Demoralizing Shout",
        "强化挫志怒吼",
    ),
    "unbridledWrath": ("Unbridled Wrath", "怒不可遏"),
    "improvedCleave": ("Improved Cleave", "强化顺劈斩"),
    "piercingHowl": ("Piercing Howl", "刺耳怒吼"),
    "bloodCraze": ("Blood Craze", "血之狂热"),
    "improvedBattleShout": (
        "Improved Battle Shout",
        "强化战斗怒吼",
        "强化怒吼",
    ),
    "dualWieldSpecialization": ("Dual Wield Specialization", "双武器专精"),
    "improvedExecute": ("Improved Execute", "强化斩杀"),
    "enrage": ("Enrage", "狂怒"),
    "improvedSlam": ("Improved Slam", "强化猛击"),
    "deathWish": ("Death Wish", "死亡之愿"),
    "improvedIntercept": ("Improved Intercept", "强化拦截"),
    "improvedBerserkerRage": ("Improved Berserker Rage", "强化狂暴之怒"),
    "flurry": ("Flurry", "乱舞"),
    "bloodthirst": ("Bloodthirst", "嗜血"),
    # Protection.
    "shieldSpecialization": ("Shield Specialization", "盾牌专精"),
    "anticipation": ("Anticipation", "预知"),
    "improvedBloodrage": ("Improved Bloodrage", "强化血性狂暴"),
    "toughness": ("Toughness", "坚韧"),
    "ironWill": ("Iron Will", "钢铁意志"),
    "lastStand": ("Last Stand", "破釜沉舟"),
    "improvedShieldBlock": ("Improved Shield Block", "强化盾牌格挡"),
    "improvedRevenge": ("Improved Revenge", "强化复仇"),
    "defiance": ("Defiance", "挑衅"),
    "improvedSunderArmor": ("Improved Sunder Armor", "强化破甲攻击"),
    "improvedDisarm": ("Improved Disarm", "强化缴械"),
    "improvedTaunt": ("Improved Taunt", "强化嘲讽"),
    "improvedShieldWall": ("Improved Shield Wall", "强化盾墙"),
    "concussionBlow": ("Concussion Blow", "震荡猛击"),
    "improvedShieldBash": ("Improved Shield Bash", "强化盾击"),
    "oneHandedWeaponSpecialization": (
        "One-Handed Weapon Specialization",
        "One Handed Weapon Specialization",
        "单手武器专精",
    ),
    "shieldSlam": ("Shield Slam", "盾牌猛击"),
}

_RAVAGER_ALIASES = ("Ravager", "Improved Whirlwind", "碾碎")
_ITEM_LINK = re.compile(r"(?:\|H)?item:(-?\d+):(-?\d+):(-?\d+)", re.IGNORECASE)


class WowsimsProfileError(ValueError):
    """The profile cannot be represented by the supported wowsims request."""


@dataclass(frozen=True)
class ProfileSelection:
    path: Path
    line_number: int
    record: dict[str, Any]
    selection_mode: str


@dataclass(frozen=True)
class WowsimsProfileResult:
    request_path: Path
    metadata_path: Path
    source_path: Path
    source_event: str
    talents_string: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "request": str(self.request_path),
            "metadata": str(self.metadata_path),
            "source": str(self.source_path),
            "source_event": self.source_event,
            "talents_string": self.talents_string,
        }


def _normalized_name(value: Any) -> str:
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", str(value).casefold())


_TALENT_BY_NAME: dict[str, tuple[int, int, str]] = {}
for _tree_index, _fields in enumerate(WARRIOR_TALENT_FIELDS):
    for _field_index, _field_name in enumerate(_fields):
        for _alias in (_field_name, *_DISPLAY_ALIASES.get(_field_name, ())):
            _TALENT_BY_NAME[_normalized_name(_alias)] = (
                _tree_index,
                _field_index,
                _field_name,
            )
_RAVAGER_NAMES = {_normalized_name(alias) for alias in _RAVAGER_ALIASES}


def _strict_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WowsimsProfileError(f"{label} must be an integer")
    return value


def _record_order_key(
    path: Path, line_number: int, record: Mapping[str, Any]
) -> tuple[str, str, int, int]:
    provenance = record.get("provenance")
    imported_at = ""
    if isinstance(provenance, Mapping):
        imported_at = str(provenance.get("imported_at") or "")
    sequence = record.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int):
        sequence = line_number
    return imported_at, path.name, sequence, line_number


def _read_jsonl(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as error:
        raise WowsimsProfileError(f"could not read {path}: {error}") from error
    for line_number, line in enumerate(source.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise WowsimsProfileError(
                f"{path}:{line_number}: invalid JSON: {error.msg}"
            ) from error
        if not isinstance(record, dict):
            raise WowsimsProfileError(
                f"{path}:{line_number}: calibration record must be an object"
            )
        yield line_number, record


def select_profile_record(paths: Iterable[Path]) -> ProfileSelection:
    """Select the newest full profile, or the newest usable old boundary."""

    static_candidates: list[
        tuple[tuple[str, str, int, int], Path, int, dict[str, Any]]
    ] = []
    boundary_candidates: list[
        tuple[tuple[str, str, int, int], Path, int, dict[str, Any]]
    ] = []
    path_count = 0
    for path in sorted((Path(value) for value in paths), key=lambda value: value.name):
        path_count += 1
        for line_number, record in _read_jsonl(path):
            state = record.get("state")
            if not isinstance(state, Mapping):
                continue
            candidate = (
                _record_order_key(path, line_number, record),
                path,
                line_number,
                record,
            )
            if (
                record.get("event") == "STATIC_PROFILE_CAPTURED"
                # Turtle can transiently emit explicit empty arrays during
                # reload while the talent/skill APIs are not populated.  Such
                # a record must not outrank the preceding complete snapshot.
                and state.get("equipment") != []
                and state.get("talents") != []
            ):
                static_candidates.append(candidate)
            elif isinstance(state.get("equipment"), list) and isinstance(
                state.get("talents"), list
            ):
                boundary_candidates.append(candidate)
    if path_count == 0:
        raise WowsimsProfileError("no calibration JSONL files were found")

    candidates = static_candidates or boundary_candidates
    if not candidates:
        raise WowsimsProfileError(
            "no full STATIC_PROFILE_CAPTURED record or equipment/talent boundary state was found"
        )
    _, path, line_number, record = max(candidates, key=lambda value: value[0])
    return ProfileSelection(
        path=path,
        line_number=line_number,
        record=record,
        selection_mode=(
            "latest_static_profile"
            if static_candidates
            else "latest_boundary_fallback"
        ),
    )


def parse_item_link(link: Any) -> dict[str, int]:
    """Convert a WoW item link into the fields accepted by ItemSpec JSON."""

    if not isinstance(link, str):
        raise WowsimsProfileError("equipment link must be a string")
    match = _ITEM_LINK.search(link)
    if match is None:
        raise WowsimsProfileError(f"could not parse equipment link: {link!r}")
    item_id, enchant_id, random_suffix = (int(value) for value in match.groups())
    if item_id <= 0:
        raise WowsimsProfileError(f"equipment item ID must be positive: {link!r}")
    item_spec = {"id": item_id}
    if enchant_id:
        item_spec["enchant"] = enchant_id
    if random_suffix:
        item_spec["randomSuffix"] = random_suffix
    return item_spec


def _convert_equipment(
    equipment: Any,
) -> tuple[list[dict[str, int]], list[dict[str, Any]]]:
    if not isinstance(equipment, list):
        raise WowsimsProfileError("state.equipment must be an array")
    items: list[dict[str, int]] = [
        {} for _ in range(WOWSIMS_EQUIPMENT_SLOT_COUNT)
    ]
    ignored: list[dict[str, Any]] = []
    observed_slots: set[int] = set()
    for index, entry in enumerate(equipment):
        if not isinstance(entry, Mapping):
            raise WowsimsProfileError(f"state.equipment[{index}] must be an object")
        slot = _strict_int(entry.get("slot"), f"state.equipment[{index}].slot")
        if slot in observed_slots:
            raise WowsimsProfileError(f"state.equipment contains duplicate WoW slot {slot}")
        observed_slots.add(slot)
        wowsims_slot = WOW_SLOT_TO_WOWSIMS_SLOT.get(slot)
        if wowsims_slot is None:
            ignored.append(
                {
                    "slot": slot,
                    "reason": "WoW shirt, tabard, bag, or unsupported inventory slot",
                }
            )
            continue
        items[wowsims_slot] = parse_item_link(entry.get("link"))
    return items, ignored


def _is_turtle_ravager(talent: Mapping[str, Any]) -> bool:
    if _normalized_name(talent.get("name")) in _RAVAGER_NAMES:
        return True
    # Turtle 1.18.x Fury tier 5 / column 1 is the three-rank Ravager talent.
    # This positional guard prevents a localized/renamed Ravager from ever
    # falling into the old wowsims Improved Slam field at the same visual slot.
    return (
        talent.get("tab") == 2
        and talent.get("tier") == 5
        and talent.get("column") == 1
        and talent.get("maxRank") == 3
    )


def warrior_talents_to_string(
    talents: Any,
) -> tuple[
    str,
    list[dict[str, Any]],
    dict[str, str],
    dict[str, int],
    list[dict[str, Any]],
]:
    """Map observed Turtle talent semantics into the existing Warrior proto."""

    if not isinstance(talents, list):
        raise WowsimsProfileError("state.talents must be an array")
    ranks = [[0 for _ in fields] for fields in WARRIOR_TALENT_FIELDS]
    populated_fields: set[str] = set()
    unmapped: list[dict[str, Any]] = []
    option_values = {"ravagerRank": 0}
    option_mappings: list[dict[str, Any]] = []

    for index, talent in enumerate(talents):
        if not isinstance(talent, Mapping):
            raise WowsimsProfileError(f"state.talents[{index}] must be an object")
        rank = _strict_int(talent.get("rank"), f"state.talents[{index}].rank")
        if rank < 0 or rank > 9:
            raise WowsimsProfileError(
                f"state.talents[{index}].rank cannot be encoded as one digit"
            )
        observed = {
            key: talent.get(key)
            for key in ("tab", "index", "tier", "column", "name", "rank", "maxRank")
            if talent.get(key) is not None
        }
        if _is_turtle_ravager(talent):
            if option_mappings:
                raise WowsimsProfileError("duplicate observed talent semantic: ravager")
            option_values["ravagerRank"] = rank
            observed.update({
                "semantic": "ravager",
                "target": "warrior.options.ravagerRank",
                "value": rank,
                "status": "applied_outside_talents_string",
                "reason": (
                    "Turtle Ravager has no position in the legacy WarriorTalents "
                    "string and is carried by the Turtle simulator option"
                ),
            })
            option_mappings.append(observed)
            continue

        mapped = _TALENT_BY_NAME.get(_normalized_name(talent.get("name")))
        if mapped is None:
            raise WowsimsProfileError(
                "learned Turtle Warrior talent has no simulator mapping: "
                f"name={talent.get('name')!r}, tab={talent.get('tab')!r}, "
                f"index={talent.get('index')!r}, rank={rank}"
            )
        tree_index, field_index, field_name = mapped
        if field_name in populated_fields:
            raise WowsimsProfileError(f"duplicate observed talent semantic: {field_name}")
        populated_fields.add(field_name)
        ranks[tree_index][field_index] = rank

    tree_strings = []
    for tree_ranks in ranks:
        tree_strings.append("".join(str(rank) for rank in tree_ranks).rstrip("0"))
    talents_string = "-".join(tree_strings).rstrip("-")
    return (
        talents_string,
        unmapped,
        {
            "arms": tree_strings[0],
            "fury": tree_strings[1],
            "protection": tree_strings[2],
        },
        option_values,
        option_mappings,
    )


def _enum_lookup(value: Any, values: Mapping[str, str]) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = re.sub(r"[^A-Z]", "", value.upper())
    for prefix in ("RACE", "CLASS"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
    return values.get(normalized)


def _template_player(request: dict[str, Any]) -> dict[str, Any]:
    try:
        player = request["raid"]["parties"][0]["players"][0]
    except (KeyError, IndexError, TypeError) as error:
        raise WowsimsProfileError(
            "template must contain raid.parties[0].players[0]"
        ) from error
    if not isinstance(player, dict):
        raise WowsimsProfileError("template player must be an object")
    return player


def build_wowsims_profile(
    template: Mapping[str, Any], selection: ProfileSelection
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return an updated request plus a separate non-proto metadata document."""

    if not isinstance(template, Mapping):
        raise WowsimsProfileError("template must be a JSON object")
    state = selection.record.get("state")
    if not isinstance(state, Mapping):
        raise WowsimsProfileError("selected calibration record has no state object")
    request = deepcopy(dict(template))
    player = _template_player(request)

    identity = state.get("characterIdentity")
    if not isinstance(identity, Mapping):
        identity = {}
    character_name = identity.get("name") or state.get("playerName")
    observed_level = identity.get("level") or state.get("playerLevel")
    race_source = identity.get("raceFile") or identity.get("raceName") or state.get("raceFile")
    class_source = identity.get("classFile") or identity.get("className") or state.get("classFile")
    race_enum = _enum_lookup(race_source, RACE_ENUMS)
    class_enum = _enum_lookup(class_source, CLASS_ENUMS)

    preserved_template_fields: list[str] = []
    unsupported_identity: list[dict[str, Any]] = []
    if isinstance(character_name, str) and character_name:
        player["name"] = character_name
    else:
        preserved_template_fields.append("player.name")
    if race_enum is None:
        raise WowsimsProfileError(
            f"live profile race was not observed or is unsupported: {race_source!r}"
        )
    if class_enum is None:
        raise WowsimsProfileError(
            f"live profile class was not observed or is unsupported: {class_source!r}"
        )
    player["race"] = race_enum
    player["class"] = class_enum

    if class_enum != "ClassWarrior":
        raise WowsimsProfileError(
            f"only Warrior talent conversion is supported, got {class_enum!r}"
        )

    equipment, ignored_slots = _convert_equipment(state.get("equipment"))
    (
        talents_string,
        unmapped_talents,
        tree_strings,
        talent_option_values,
        talent_option_mappings,
    ) = warrior_talents_to_string(state.get("talents"))
    player["equipment"] = {"items": equipment}
    player["talentsString"] = talents_string
    warrior = player.get("warrior")
    if not isinstance(warrior, dict):
        raise WowsimsProfileError("template Warrior player is missing warrior options")
    options = warrior.get("options")
    if not isinstance(options, dict):
        raise WowsimsProfileError("template Warrior player is missing warrior.options")
    options["ravagerRank"] = talent_option_values["ravagerRank"]

    improved_slam_tree = tree_strings["fury"]
    improved_slam_rank = (
        int(improved_slam_tree[11]) if len(improved_slam_tree) > 11 else 0
    )

    static_counts = {}
    for field in (
        "equipment",
        "talents",
        "spellbook",
        "actionBarSpells",
        "skillLines",
        "bagItems",
    ):
        value = state.get(field)
        static_counts[field] = len(value) if isinstance(value, list) else None

    provenance = selection.record.get("provenance")
    metadata = {
        "schema_version": 1,
        "kind": "brainofcat_wowsims_profile_metadata",
        "created_at": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "source": {
            "calibration_jsonl": str(selection.path),
            "line_number": selection.line_number,
            "event": selection.record.get("event"),
            "sequence": selection.record.get("sequence"),
            "selection_mode": selection.selection_mode,
            "imported_at": (
                provenance.get("imported_at")
                if isinstance(provenance, Mapping)
                else None
            ),
        },
        "observed_character": {
            "identity": dict(identity),
            "level": observed_level,
            "static_counts": static_counts,
        },
        "mapped": {
            "name": player.get("name"),
            "race": player.get("race"),
            "class": player.get("class"),
            "equipment_slots": len(equipment),
            "talents_string": talents_string,
            "talent_trees": tree_strings,
        },
        "unmapped_talents": unmapped_talents,
        "talent_option_mappings": talent_option_mappings,
        "simulator_semantic_defaults": [
            {
                "field": "WarriorTalents.improvedSlam",
                "value": improved_slam_rank,
                "reason": (
                    "Value reflects only the observed Improved Slam semantic; "
                    "Turtle Ravager is mapped separately to "
                    "warrior.options.ravagerRank"
                ),
            }
        ],
        "unsupported_identity": unsupported_identity,
        "ignored_inventory_slots": ignored_slots,
        "preserved_template_fields": preserved_template_fields,
        "preserved_template_scope": (
            "encounter, raid/party buffs and debuffs, player rotation, consumes, "
            "cooldowns, and existing Warrior options; ravagerRank is added from "
            "the observed Turtle talent"
        ),
        "level_mapping": {
            "observed": observed_level,
            "status": "metadata_only_player_proto_has_no_level_field",
        },
    }
    return request, metadata


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise WowsimsProfileError(f"could not read {label} {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise WowsimsProfileError(
            f"invalid JSON in {label} {path}: {error.msg}"
        ) from error
    if not isinstance(value, dict):
        raise WowsimsProfileError(f"{label} must be a JSON object")
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as temporary:
        temporary.write(rendered)
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)


def generate_wowsims_profile(
    calibration_paths: Iterable[Path],
    *,
    template_path: Path = DEFAULT_TEMPLATE,
    output_path: Path = DEFAULT_OUTPUT,
    metadata_path: Path | None = None,
) -> WowsimsProfileResult:
    selection = select_profile_record(calibration_paths)
    if selection.selection_mode != "latest_static_profile":
        raise WowsimsProfileError(
            "no STATIC_PROFILE_CAPTURED record was found; refusing to build a "
            "live character request from a partial boundary state"
        )
    template = _load_json_object(template_path, "template")
    request, metadata = build_wowsims_profile(template, selection)
    resolved_metadata_path = metadata_path or output_path.with_suffix(".metadata.json")
    metadata["template"] = str(template_path)
    metadata["output_request"] = str(output_path)
    _write_json(output_path, request)
    _write_json(resolved_metadata_path, metadata)
    return WowsimsProfileResult(
        request_path=output_path,
        metadata_path=resolved_metadata_path,
        source_path=selection.path,
        source_event=str(selection.record.get("event") or ""),
        talents_string=str(metadata["mapped"]["talents_string"]),
    )


def _source_paths(source: Path | None, calibration_root: Path) -> list[Path]:
    if source is None:
        return sorted(calibration_root.glob("*.jsonl"))
    if source.is_dir():
        return sorted(source.glob("*.jsonl"))
    return [source]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "calibration_jsonl",
        nargs="?",
        type=Path,
        help=(
            "one imported calibration JSONL, or a directory; if omitted, "
            "scan offline_data/calibration"
        ),
    )
    parser.add_argument("--calibration-root", type=Path, default=DEFAULT_CALIBRATION_ROOT)
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--metadata-output", type=Path)
    args = parser.parse_args(argv)

    try:
        result = generate_wowsims_profile(
            _source_paths(args.calibration_jsonl, args.calibration_root),
            template_path=args.template,
            output_path=args.output,
            metadata_path=args.metadata_output,
        )
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
        return 0
    except (OSError, WowsimsProfileError) as error:
        print(f"wowsims profile build failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
