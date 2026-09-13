from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import unittest

from o2o_dps.historical_source_build_aura_match_v1 import (
    HistoricalSourceAuraMatchError,
    build_ready_source_aura_development_pair,
    match_source_build_aura_bundle,
    match_source_build_auras,
)
from o2o_dps.fury_paired_multiseed_runner_v4 import sha256_json


def _request() -> dict:
    return {
        "segment_ref": "segment-a",
        "causal_source_identity": {
            "identity": {
                "instance_id": "instance-a",
                "player_guid": "0xABCD",
            },
            # A build segment can carry forward into a later encounter.
            "valid_from": {
                "encounter_id": "earlier-encounter", "timestamp_ms": 100,
                "event_index": 1,
            },
        },
        "composition": {
            "request": {
                "raid": {
                    "parties": [{"players": [{
                        "class": "ClassWarrior", "name": "Warrior",
                        "equipment": {"items": [{"id": 1}]},
                        "talentsString": "12345",
                        "consumes": {}, "buffs": {},
                    }]}],
                    "buffs": {},
                },
            },
        },
    }


def _checkpoint() -> dict:
    def aura(spell_id: int, spell: str) -> dict:
        return {
            "spell_id": spell_id, "spell": spell, "is_buff": True,
            "remaining_duration_status": "MISSING_NOT_IN_AURA_PROTO",
            "last_transition": {"timestamp_ms": 150, "event_index": 5},
        }

    return {
        "segment_ref": "segment-a",
        "source_identity": {
            "instance_id": "instance-a", "encounter_id": "later-encounter",
            "player_guid": "0xabcd",
        },
        "window_start": {"cutoff_exclusive_order_key": [200, 10]},
        "strict_prefix_input": {"future_suffix_used": False},
        "fields": {"player.self_auras_and_procs": {
            "observation_category": "PARTIAL",
            "value": {"observed_active": [
                aura(11405, "Elixir of the Giants"),
                aura(17538, "Elixir of the Mongoose"),
                aura(24932, "Leader of the Pack"),
                aura(24799, "Well Fed"),
            ]},
        }},
    }


class HistoricalSourceAuraMatchTests(unittest.TestCase):
    def test_default_diagnostic_does_not_reinterpret_empty_maps_as_absent(self) -> None:
        source = _request()
        matched = match_source_build_auras(source, _checkpoint())
        player = matched["request"]["raid"]["parties"][0]["players"][0]
        self.assertEqual(player["consumes"], {})
        self.assertEqual(matched["request"]["raid"]["buffs"], {})
        self.assertEqual(matched["simulator_supported_at_cutoff"]["consumes"], {
            "strengthBuff": "ElixirOfGiants",
            "agilityElixir": "ElixirOfTheMongoose",
        })
        self.assertEqual(
            [a["spell"] for a in matched["unmapped_active_self_buffs_at_cutoff"]],
            ["Well Fed"],
        )
        self.assertEqual(matched["unobserved_effects_status"], "UNKNOWN_NOT_PROVEN_ABSENT")
        self.assertFalse(matched["applied_to_request"])
        self.assertFalse(matched["comparison_eligible"])
        self.assertEqual(source, _request())

    def test_explicit_window_persistence_injects_only_supported_fields(self) -> None:
        matched = match_source_build_auras(
            _request(), _checkpoint(), assume_persists_through_window=True
        )
        raid = matched["request"]["raid"]
        player = raid["parties"][0]["players"][0]
        self.assertEqual(player["consumes"]["strengthBuff"], "ElixirOfGiants")
        self.assertEqual(raid["buffs"], {"leaderOfThePack": True})
        self.assertEqual(player["equipment"], {"items": [{"id": 1}]})
        self.assertEqual(player["talentsString"], "12345")
        self.assertEqual(matched["whole_window_persistence"], "EXPLICIT_DEVELOPMENT_ASSUMPTION")

    def test_cross_encounter_build_is_allowed_but_future_build_is_not(self) -> None:
        match_source_build_auras(_request(), _checkpoint())
        source = _request()
        source["causal_source_identity"]["valid_from"]["timestamp_ms"] = 201
        with self.assertRaisesRegex(HistoricalSourceAuraMatchError, "starts after"):
            match_source_build_auras(source, _checkpoint())
        source["causal_source_identity"]["valid_from"].update(
            timestamp_ms=200, event_index=10
        )
        with self.assertRaisesRegex(HistoricalSourceAuraMatchError, "starts after"):
            match_source_build_auras(source, _checkpoint())

    def test_wrong_player_or_segment_is_not_joined(self) -> None:
        checkpoint = _checkpoint()
        checkpoint["source_identity"]["player_guid"] = "0x9999"
        with self.assertRaisesRegex(HistoricalSourceAuraMatchError, "player_guid mismatch"):
            match_source_build_auras(_request(), checkpoint)
        checkpoint = _checkpoint()
        checkpoint["segment_ref"] = "segment-b"
        with self.assertRaisesRegex(HistoricalSourceAuraMatchError, "segment mismatch"):
            match_source_build_auras(_request(), checkpoint)

    def test_future_aura_is_rejected_even_when_checkpoint_claims_prefix(self) -> None:
        checkpoint = _checkpoint()
        checkpoint["fields"]["player.self_auras_and_procs"]["value"]["observed_active"][0][
            "last_transition"
        ]["timestamp_ms"] = 201
        with self.assertRaisesRegex(HistoricalSourceAuraMatchError, "strict-prefix"):
            match_source_build_auras(_request(), checkpoint)

    def test_bundle_requires_one_checkpoint_per_source_segment(self) -> None:
        source = {"requests": [_request()]}
        checkpoint = {"rows": [_checkpoint()]}
        self.assertEqual(len(match_source_build_aura_bundle(source, checkpoint)), 1)
        duplicate = deepcopy(checkpoint)
        duplicate["rows"].append(deepcopy(duplicate["rows"][0]))
        with self.assertRaisesRegex(HistoricalSourceAuraMatchError, "duplicate"):
            match_source_build_aura_bundle(source, duplicate)
        with self.assertRaisesRegex(HistoricalSourceAuraMatchError, "missing"):
            match_source_build_aura_bundle(source, {"rows": []})


class RealShapeDevelopmentPairTests(unittest.TestCase):
    def test_unique_ready_pair_rebinds_only_raid_effects(self) -> None:
        derived_root = Path(__file__).resolve().parents[1] / "offline_data" / "derived"
        paths = {
            "source": derived_root / "historical_fury_source_bound_prototype_bundle/v1/manifest.json",
            "checkpoint": derived_root / "historical_fury_source_bound_prefix_checkpoint/v1/manifest.json",
            "hypothesis": derived_root / "historical_fury_source_bound_dynamic_hypothesis/v2/manifest.json",
        }
        if any(not path.is_file() for path in paths.values()):
            self.skipTest("local frozen source-bound manifests are not installed")
        documents = {
            name: json.loads(path.read_text(encoding="utf-8"))
            for name, path in paths.items()
        }
        frozen = deepcopy(documents["hypothesis"])
        result = build_ready_source_aura_development_pair(
            documents["source"], documents["checkpoint"], documents["hypothesis"]
        )
        ready = [
            row for row in frozen["requests"]
            if row["status"] == "READY_FOR_LOCAL_NATIVE_WIRE_SMOKE_ONLY"
        ]
        self.assertEqual(len(ready), 1)
        old = ready[0]["derived_request_template"]
        new = result["request"]
        self.assertEqual(new["encounter"], old["encounter"])
        self.assertEqual(new["simOptions"], old["simOptions"])
        self.assertNotEqual(new["raid"], old["raid"])
        old_player = old["raid"]["parties"][0]["players"][0]
        new_player = new["raid"]["parties"][0]["players"][0]
        self.assertEqual(new_player["equipment"], old_player["equipment"])
        self.assertEqual(new_player["talentsString"], old_player["talentsString"])
        self.assertEqual(new_player["consumes"], {
            "strengthBuff": "ElixirOfGiants",
            "agilityElixir": "ElixirOfTheMongoose",
        })
        self.assertEqual(new["raid"]["buffs"], {
            "leaderOfThePack": True, "emeraldBlessing": True,
        })
        binding = result["pair_binding"]
        self.assertEqual(binding["aura_overlay_derived_request_sha256"], sha256_json(new))
        self.assertNotEqual(
            binding["aura_overlay_derived_request_sha256"],
            ready[0]["derived_request_template_sha256"],
        )
        self.assertEqual(
            binding["dynamic_load_config_content_sha256"],
            ready[0]["dynamic_load_config"]["content_sha256"],
        )
        self.assertEqual(documents["hypothesis"], frozen)
        self.assertFalse(result["comparison_eligible"])
        self.assertFalse(result["training_authorized"])
        self.assertFalse(result["hpc_authorized"])


if __name__ == "__main__":
    unittest.main()
