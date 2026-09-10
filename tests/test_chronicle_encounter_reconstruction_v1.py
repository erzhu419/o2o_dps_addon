from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps.chronicle_encounter_reconstruction_v1 import (
    ReconstructionError,
    build_npc_identity_aggregates,
    iter_reconstructed_encounters,
    reconstruct_file,
)


PLAYER = "0x0000000000000001"
MOB_A = "0xF130000001000001"
MOB_B = "0xF130000001000002"
MOB_C = "0xF130000003000003"
FRIENDLY_TOTEM = "0xF130001CF8000004"
FRIENDLY_OWNED_PET = "0xF130004364000005"
FRIENDLY_OWNED_OTHER = "0xF130000999000006"


def row(
    index: int,
    offset_ms: int,
    event_type: str,
    *,
    encounter: str = "enc-1",
    source: str | None = None,
    source_guid: str | None = None,
    target: str | None = None,
    target_guid: str | None = None,
    spell: str | None = None,
    spell_id: int | None = None,
    value: int | str | None = None,
    outcome: str | None = None,
) -> dict[str, object]:
    return {
        "instance": "instance-1",
        "encounter": encounter,
        "event_index": index,
        "offset_ms": offset_ms,
        "time": f"00:00:{offset_ms / 1000:06.3f}",
        "type": event_type,
        "source": source,
        "source_guid": source_guid,
        "target": target,
        "target_guid": target_guid,
        "spell": spell,
        "spell_id": spell_id,
        "value": value,
        "outcome": outcome,
        "flags": [],
        "activity": None,
        "provenance": {
            "source_file": "fixture.csv",
            "csv_line": index + 2,
            "export_row": index,
        },
    }


class ChronicleEncounterReconstructionV1Tests(unittest.TestCase):
    def test_dead_row_is_not_counted_as_unparsed_damage(self) -> None:
        rows = [
            row(
                0,
                0,
                "CLASS",
                target="Warrior",
                target_guid=PLAYER,
                outcome="Friendly Player",
            ),
            row(
                1,
                0,
                "CLASS",
                target="Mob A",
                target_guid=MOB_A,
                outcome="Hostile Creature",
            ),
            row(
                2,
                1_000,
                "DMG",
                source="Warrior",
                source_guid=PLAYER,
                target="Mob A",
                target_guid=MOB_A,
                value=100,
                outcome="HIT",
            ),
            row(3, 2_000, "DEAD", target="Mob A", target_guid=MOB_A),
        ]
        target = list(iter_reconstructed_encounters(rows))[0]["waves"][0]["targets"][0]
        self.assertEqual(target["observed_incoming_damage_sum"]["unparsed_event_count"], 0)
        self.assertEqual(
            target["kill_budget_proxy"]["completeness"],
            "COMPLETE_FOR_NORMALIZED_DAMAGE_ROWS",
        )

    def test_waves_targets_armor_and_melee_cohit_are_provenance_bounded(self) -> None:
        rows = [
            row(
                0,
                0,
                "CLASS",
                target="Warrior",
                target_guid=PLAYER,
                outcome="Friendly Player",
            ),
            row(
                1,
                0,
                "CLASS",
                target="Mob A",
                target_guid=MOB_A,
                outcome="Hostile Creature",
            ),
            row(
                2,
                0,
                "CLASS",
                target="Mob B",
                target_guid=MOB_B,
                outcome="Hostile Creature",
            ),
            row(
                3,
                1_000,
                "DMG",
                source="Warrior",
                source_guid=PLAYER,
                target="Mob A",
                target_guid=MOB_A,
                spell="Whirlwind",
                spell_id=1680,
                value=100,
                outcome="Hit · Physical",
            ),
            row(
                4,
                1_000,
                "DMG",
                source="Warrior",
                source_guid=PLAYER,
                target="Mob B",
                target_guid=MOB_B,
                spell="Whirlwind",
                spell_id=1680,
                value=120,
                outcome="Hit · Physical",
            ),
            row(
                5,
                1_200,
                "START",
                source="Mob A",
                source_guid=MOB_A,
                target="Warrior",
                target_guid=PLAYER,
                spell="Claw",
                spell_id=1,
                value="—",
                outcome="cast=0ms",
            ),
            row(
                6,
                1_300,
                "AURA",
                target="Mob A",
                target_guid=MOB_A,
                spell="Sunder Armor",
                spell_id=11597,
                value=3,
                outcome="Added (stacks=3)",
            ),
            row(
                7,
                1_500,
                "HEAL",
                source="Mob B",
                source_guid=MOB_B,
                target="Mob A",
                target_guid=MOB_A,
                spell="Heal",
                value=20,
                outcome="Hit · Nature",
            ),
            row(
                8,
                1_700,
                "AURA",
                target="Mob A",
                target_guid=MOB_A,
                spell="Sunder Armor",
                spell_id=11597,
                value=5,
                outcome="Increased (stacks=5)",
            ),
            row(
                9,
                2_000,
                "DMG",
                source="Warrior",
                source_guid=PLAYER,
                target="Mob A",
                target_guid=MOB_A,
                spell="Bloodthirst",
                spell_id=23894,
                value=500,
                outcome="Crit · Physical",
            ),
            row(
                10,
                2_001,
                "DEAD",
                source="Warrior",
                source_guid=PLAYER,
                target="Mob A",
                target_guid=MOB_A,
                spell="Execute",
                value=50,
                outcome="Hit · Physical",
            ),
            row(
                11,
                2_100,
                "DMG",
                source="Warrior",
                source_guid=PLAYER,
                target="Mob B",
                target_guid=MOB_B,
                spell="Bloodthirst",
                spell_id=23894,
                value=400,
                outcome="Hit · Physical",
            ),
            row(12, 2_101, "DEAD", target="Mob B", target_guid=MOB_B),
            row(
                13,
                12_000,
                "DMG",
                source="Warrior",
                source_guid=PLAYER,
                target="Mob C",
                target_guid=MOB_C,
                spell="Bloodthirst",
                spell_id=23894,
                value=300,
                outcome="Hit · Physical",
            ),
            row(14, 12_001, "DEAD", target="Mob C", target_guid=MOB_C),
        ]

        encounters = list(iter_reconstructed_encounters(rows))
        self.assertEqual(len(encounters), 1)
        encounter = encounters[0]
        self.assertEqual(encounter["wave_count"], 2)
        first = encounter["waves"][0]
        self.assertEqual(first["target_count"], 2)
        self.assertEqual(first["position_assumption"]["value"], "stacked")
        self.assertEqual(
            first["same_batch_damage_groups"][0]["relation"],
            "MELEE_REACH_COHIT",
        )
        mob_a = next(
            value for value in first["targets"] if value["target_guid"] == MOB_A
        )
        self.assertEqual(mob_a["classification"]["status"], "OBSERVED")
        self.assertEqual(mob_a["observed_incoming_damage_sum"]["value"], 650)
        self.assertEqual(mob_a["observed_healing_received_sum"]["value"], 20)
        self.assertEqual(mob_a["kill_budget_proxy"]["value"], 650)
        self.assertEqual(
            mob_a["confirmed_single_hit_health_lower_bound"]["value"], 100
        )
        armor = mob_a["armor_debuff_evidence"]
        self.assertEqual(armor["status"], "RECONSTRUCTED")
        self.assertEqual(len(armor["transitions"]), 2)
        self.assertEqual(armor["strata"][0]["effective_armor"]["status"], "MISSING")
        self.assertEqual(first["separate_position"]["status"], "MISSING")

        second = encounter["waves"][1]
        self.assertEqual(second["target_count"], 1)
        self.assertEqual(second["position_assumption"]["value"], "unknown")

    def test_generic_aoe_is_temporal_not_spatial_evidence(self) -> None:
        rows = [
            row(
                0,
                0,
                "CLASS",
                target="Paladin",
                target_guid=PLAYER,
                outcome="Friendly Player",
            ),
            row(
                1,
                100,
                "DMG",
                source="Paladin",
                source_guid=PLAYER,
                target="Mob A",
                target_guid=MOB_A,
                spell="Consecration",
                spell_id=20924,
                value=50,
            ),
            row(
                2,
                100,
                "DMG",
                source="Paladin",
                source_guid=PLAYER,
                target="Mob B",
                target_guid=MOB_B,
                spell="Consecration",
                spell_id=20924,
                value=50,
            ),
        ]
        wave = list(iter_reconstructed_encounters(rows))[0]["waves"][0]
        self.assertEqual(
            wave["same_batch_damage_groups"][0]["relation"], "TEMPORAL_COHIT_ONLY"
        )
        self.assertEqual(wave["position_assumption"]["value"], "unknown")
        self.assertEqual(len(wave["target_groups"]), 2)

    def test_friendly_totem_is_not_a_target_and_direct_player_damage_can_infer_hostile(self) -> None:
        rows = [
            row(
                0,
                0,
                "CLASS",
                target="Warrior",
                target_guid=PLAYER,
                outcome="Friendly Player",
            ),
            row(
                1,
                0,
                "CLASS",
                target="Mob A",
                target_guid=MOB_A,
                outcome="Hostile Creature",
            ),
            row(
                2,
                10,
                "CLASS",
                target="Mana Spring Totem IV",
                target_guid=FRIENDLY_TOTEM,
                outcome="Friendly Creature owner=000001",
            ),
            row(
                3,
                100,
                "DMG",
                source="Mana Spring Totem IV",
                source_guid=FRIENDLY_TOTEM,
                target="Mob A",
                target_guid=MOB_A,
                spell="Totem Pulse",
                value=10,
            ),
            row(
                4,
                200,
                "DMG",
                source="Warrior",
                source_guid=PLAYER,
                target="Unclassified Enemy",
                target_guid=MOB_C,
                spell="Bloodthirst",
                value=100,
            ),
        ]
        encounter = list(iter_reconstructed_encounters(rows))[0]
        wave = encounter["waves"][0]
        target_guids = {value["target_guid"] for value in wave["targets"]}
        self.assertEqual(target_guids, {MOB_A, MOB_C})
        self.assertNotIn(FRIENDLY_TOTEM, target_guids)
        inferred = next(
            value for value in wave["targets"] if value["target_guid"] == MOB_C
        )
        self.assertEqual(inferred["classification"]["status"], "INFERRED")
        self.assertEqual(
            inferred["classification"]["evidence"],
            "DIRECT_DAMAGE_FROM_CLASS_FRIENDLY_PLAYER",
        )
        summary = encounter["classification_summary"]
        self.assertEqual(summary["friendly_entity_count"], 1)
        self.assertEqual(summary["explicit_hostile_creature_count"], 1)
        self.assertEqual(summary["inferred_hostile_creature_count"], 1)
        self.assertEqual(summary["creature_guid_only_hostile_count"], 0)

    def test_hostile_class_label_is_overridden_by_friendly_player_owner(self) -> None:
        rows = [
            row(
                0,
                0,
                "CLASS",
                target="Felguard",
                target_guid=FRIENDLY_OWNED_OTHER,
                outcome="Hostile Creature owner=000001",
            ),
            row(
                1,
                0,
                "CLASS",
                target="Mob A",
                target_guid=MOB_A,
                outcome="Hostile Creature",
            ),
            row(
                2,
                10,
                "CLASS",
                target="Warrior",
                target_guid=PLAYER,
                outcome="Friendly Player",
            ),
            row(
                3,
                100,
                "DMG",
                source="Felguard",
                source_guid=FRIENDLY_OWNED_OTHER,
                target="Mob A",
                target_guid=MOB_A,
                spell="Cleave",
                value=100,
            ),
        ]
        encounter = list(iter_reconstructed_encounters(rows))[0]
        targets = encounter["waves"][0]["targets"]
        self.assertEqual([value["target_guid"] for value in targets], [MOB_A])
        self.assertEqual(
            encounter["classification_summary"][
                "hostile_label_overridden_by_friendly_owner_count"
            ],
            1,
        )

    def test_known_felguard_summon_is_excluded_when_owner_is_missing(self) -> None:
        rows = [
            row(
                0,
                0,
                "CLASS",
                target="Warrior",
                target_guid=PLAYER,
                outcome="Friendly Player",
            ),
            row(
                1,
                0,
                "CLASS",
                target="Felguard",
                target_guid=FRIENDLY_OWNED_PET,
                outcome="Hostile Creature",
            ),
            row(
                2,
                0,
                "CLASS",
                target="Mob A",
                target_guid=MOB_A,
                outcome="Hostile Creature",
            ),
            row(
                3,
                100,
                "DMG",
                source="Felguard",
                source_guid=FRIENDLY_OWNED_PET,
                target="Mob A",
                target_guid=MOB_A,
                spell="Cleave",
                value=100,
            ),
            row(
                4,
                200,
                "DMG",
                source="Warrior",
                source_guid=PLAYER,
                target="Mob A",
                target_guid=MOB_A,
                value=100,
            ),
        ]
        encounter = list(iter_reconstructed_encounters(rows))[0]
        targets = encounter["waves"][0]["targets"]
        self.assertEqual([value["target_guid"] for value in targets], [MOB_A])
        self.assertEqual(
            encounter["classification_summary"][
                "known_player_controlled_summon_exclusion_count"
            ],
            1,
        )

    def test_repeated_name_creates_inferred_scenario_but_single_sample_does_not(self) -> None:
        encounter_rows = [
            row(
                0,
                0,
                "CLASS",
                encounter="enc-1",
                target="Warrior",
                target_guid=PLAYER,
                outcome="Friendly Player",
            ),
            row(
                1,
                100,
                "DMG",
                encounter="enc-1",
                source="Warrior",
                source_guid=PLAYER,
                target="Repeated Mob",
                target_guid=MOB_A,
                value=500,
            ),
            row(2, 101, "DEAD", encounter="enc-1", target="Repeated Mob", target_guid=MOB_A, value=0),
            row(
                20,
                0,
                "CLASS",
                encounter="enc-2",
                target="Warrior",
                target_guid=PLAYER,
                outcome="Friendly Player",
            ),
            row(
                3,
                100,
                "DMG",
                encounter="enc-2",
                source="Warrior",
                source_guid=PLAYER,
                target="Repeated Mob",
                target_guid=MOB_B,
                value=700,
            ),
            row(4, 101, "DEAD", encounter="enc-2", target="Repeated Mob", target_guid=MOB_B, value=0),
            row(
                40,
                0,
                "CLASS",
                encounter="enc-3",
                target="Warrior",
                target_guid=PLAYER,
                outcome="Friendly Player",
            ),
            row(
                5,
                100,
                "DMG",
                encounter="enc-3",
                source="Warrior",
                source_guid=PLAYER,
                target="Single Mob",
                target_guid=MOB_C,
                value=300,
            ),
            row(6, 101, "DEAD", encounter="enc-3", target="Single Mob", target_guid=MOB_C, value=0),
        ]
        encounters = list(iter_reconstructed_encounters(encounter_rows))
        aggregates = build_npc_identity_aggregates(encounters)
        repeated = next(
            value
            for value in aggregates
            if value["identity"]["target_name"] == "Repeated Mob"
        )
        single = next(
            value
            for value in aggregates
            if value["identity"]["target_name"] == "Single Mob"
        )
        self.assertEqual(repeated["kill_budget_proxy_summary"]["count"], 2)
        self.assertEqual(repeated["health_scenario"]["status"], "INFERRED")
        self.assertEqual(repeated["health_scenario"]["center"], 600)
        self.assertEqual(single["health_scenario"]["status"], "MISSING")

    def test_reappearing_noncontiguous_encounter_is_merged_without_row_copy(self) -> None:
        rows = [
            row(1, 0, "DMG", encounter="enc-1", target="Mob A", target_guid=MOB_A, value=1),
            row(2, 0, "DMG", encounter="enc-2", target="Mob B", target_guid=MOB_B, value=1),
            row(3, 1, "DMG", encounter="enc-1", target="Mob A", target_guid=MOB_A, value=1),
        ]
        encounters = list(iter_reconstructed_encounters(rows))
        self.assertEqual([value["encounter"] for value in encounters], ["enc-1", "enc-2"])
        self.assertEqual(encounters[0]["row_count"], 2)
        self.assertEqual(encounters[1]["row_count"], 1)

    def test_file_reconstruction_is_bounded_and_writes_no_row_copy(self) -> None:
        rows = [
            row(1, 0, "DMG", encounter="enc-1", target="Mob A", target_guid=MOB_A, value=1),
            row(2, 0, "DMG", encounter="enc-2", target="Mob B", target_guid=MOB_B, value=1),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "normalized.jsonl"
            with source.open("w", encoding="utf-8", newline="\n") as handle:
                for value in rows:
                    handle.write(json.dumps(value) + "\n")
            report = reconstruct_file(source, max_encounters=1)
        self.assertEqual(report["summary"]["encounter_count"], 1)
        self.assertEqual(report["source"]["scope"], "selected_encounters")
        self.assertTrue(report["source"]["complete_file_scanned"])
        self.assertFalse(report["source"]["row_level_copy_written"])
        self.assertEqual(report["provenance_contract"]["authorship"].split(";")[0], "MISSING")


if __name__ == "__main__":
    unittest.main()
