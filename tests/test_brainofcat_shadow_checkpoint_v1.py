from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps import brainofcat_shadow_checkpoint_v1 as checkpoint


PLAYER = "0x00000000000000AB"
TARGET = "0xF130000001000001"
OTHER = "0x00000000000000CD"


def _field(quality: str = "MISSING", value: object = None) -> dict[str, object]:
    item: dict[str, object] = {"quality": quality, "source": "Nampower client API"}
    if value is not None:
        item["value"] = value
    return item


def _base(kind: str = "checkpoint", ordinal: int = 10) -> dict[str, object]:
    row: dict[str, object] = {
        "schema": checkpoint.SCHEMA,
        "kind": kind,
        "sessionId": "session-1",
        "characterKey": "Warrior-Realm",
        "pullId": "pull-1",
        "at": {"epochSeconds": 1_800_000_000, "getTimeSeconds": 123.5,
               "causalOrdinal": ordinal},
        "playerGuid": PLAYER,
        "instance": {"zone": "Upper Karazhan", "subZone": "", "idQuality": "MISSING"},
        "trigger": "decision_boundary",
    }
    if kind == "checkpoint":
        row["targetGuid"] = TARGET
        row["targets"] = [{"guid": TARGET, "maxHealth": 10000,
                           "currentHealth": 9000, "attackable": True}]
        row["fields"] = {name: _field() for name in checkpoint.REQUIRED_FIELDS}
        row["fields"]["target.max_health"] = _field("EXACT", 10000)
        row["fields"]["target.current_health"] = _field("EXACT", 9000)
        row["fields"]["player.rage_current"] = _field("EXACT", 40)
        row["fields"]["player.stance"] = _field("EXACT", "BERSERKER")
        row["fields"]["player.self_auras_and_procs"] = _field(
            "PARTIAL", [{"spellId": 12966, "stacks": 3, "texture": "Flurry"}]
        )
    elif kind == "event_delta":
        del row["instance"]
        row["event"] = {"name": "SPELL_DAMAGE", "kind": "DMG", "sourceGuid": PLAYER,
                        "targetGuid": TARGET, "spellId": 23881, "amount": 1800}
    else:
        del row["pullId"]
        del row["instance"]
        row["equipment"] = []
        row["talents"] = []
        row["clientBuild"] = {"build": "7272"}
    return row


def _chronicle(timestamp: int, *, amount: int = 1800, event_index: int = 1,
               player: str = PLAYER, encounter: str = "enc-1") -> dict[str, object]:
    return {"instance": "inst-1", "encounter": encounter, "timestamp_ms": timestamp,
            "event_index": event_index, "type": "DMG", "source_guid": player,
            "target_guid": TARGET, "spell_id": 23881, "value": amount}


class ShadowCheckpointV1Tests(unittest.TestCase):
    def test_client_combat_message_is_kept_but_not_a_server_anchor(self) -> None:
        row = _base("event_delta", 12)
        row["event"] = {
            "name": "CHAT_MSG_COMBAT_SELF_HITS",
            "kind": "CLIENT_LOG_RESULT",
            "message": "client white-result text",
        }
        validated = checkpoint.validate_record(row)
        self.assertEqual(validated["event"]["kind"], "CLIENT_LOG_RESULT")
        self.assertIsNone(checkpoint._signature(validated, chronicle=False))

    def test_partial_client_checkpoint_is_retained_without_default_fill(self) -> None:
        validated = checkpoint.validate_record(_base())
        self.assertFalse(validated["exactCheckpointReady"])
        self.assertIn("queue.next_swing", validated["exactCheckpointBlockers"])
        self.assertIn("target.registry_scope", validated["exactCheckpointBlockers"])
        self.assertNotIn("value", validated["fields"]["queue.next_swing"])

    def test_absent_state_and_future_source_cannot_be_promoted(self) -> None:
        for mutation in ("missing_value", "future", "unknown_field"):
            with self.subTest(mutation=mutation):
                row = _base()
                fields = row["fields"]
                assert isinstance(fields, dict)
                if mutation == "missing_value":
                    fields["queue.next_swing"]["value"] = "NONE"
                elif mutation == "future":
                    fields["player.rage_current"]["source"] = "FUTURE_SUFFIX"
                else:
                    del fields["player.stance"]
                with self.assertRaises(checkpoint.ShadowCheckpointError):
                    checkpoint.validate_record(row)

    def test_current_health_must_not_exceed_maximum_when_exact(self) -> None:
        row = _base()
        row["fields"]["target.current_health"]["value"] = 10001
        row["targets"][0]["currentHealth"] = 10001
        with self.assertRaisesRegex(checkpoint.ShadowCheckpointError, "exceeds maximum"):
            checkpoint.validate_record(row)

    def test_exact_health_must_match_target_observation(self) -> None:
        row = _base()
        row["targets"][0]["currentHealth"] = 8500
        with self.assertRaisesRegex(checkpoint.ShadowCheckpointError, "disagrees"):
            checkpoint.validate_record(row)
        row = _base()
        row["exactCheckpointReady"] = True
        with self.assertRaisesRegex(checkpoint.ShadowCheckpointError, "cannot claim"):
            checkpoint.validate_record(row)

    def test_unique_guid_and_second_is_only_coarse(self) -> None:
        row = _base()
        t = 1_800_000_000_000
        events = [_chronicle(t - 10, amount=1), _chronicle(t + 1010, amount=2)]
        result = checkpoint.join_chronicle_encounter(row, iter(events))
        self.assertEqual(result["status"], "COARSE_UNIQUE_TIME_GUID")
        self.assertFalse(result["exact_event_order_equivalent"])
        self.assertEqual(result["source_identity"]["encounter_id"], "enc-1")

    def test_client_observed_bracket_does_not_prove_server_cutoff(self) -> None:
        row = _base()
        before = _base("event_delta", 9)
        after = _base("event_delta", 11)
        after["event"]["amount"] = 1950
        t = 1_800_000_000_000
        events = [_chronicle(t - 10), _chronicle(t + 1010, amount=1950,
                                                 event_index=2)]
        result = checkpoint.join_chronicle_encounter(
            row, iter(events), local_records=[before, after]
        )
        self.assertEqual(result["status"], "CLIENT_OBSERVED_EVENT_BRACKET")
        self.assertFalse(result["exact_event_order_equivalent"])
        self.assertEqual(result["chronicle_order_bracket"]["before"], [t - 10, 1])
        duplicate = events + [_chronicle(t + 1011, event_index=3)]
        result = checkpoint.join_chronicle_encounter(
            row, iter(duplicate), local_records=[before, after]
        )
        self.assertFalse(result["exact_event_order_equivalent"])

    def test_identity_and_time_ambiguity_do_not_join(self) -> None:
        row = _base()
        t = 1_800_000_000_000
        other_player = [
            _chronicle(t - 10, player=OTHER),
            _chronicle(t + 1010, amount=2, player=OTHER),
        ]
        self.assertEqual(checkpoint.join_chronicle_encounter(row, other_player)["status"],
                         "NO_TIME_GUID_MATCH")
        ambiguous = [
            _chronicle(t - 10), _chronicle(t + 1010, amount=2),
            _chronicle(t - 10, encounter="enc-2"),
            _chronicle(t + 1010, amount=2, encounter="enc-2"),
        ]
        self.assertEqual(checkpoint.join_chronicle_encounter(row, ambiguous)["status"],
                         "AMBIGUOUS_TIME_GUID")

    def test_import_streams_chronicle_and_preserves_event_binding(self) -> None:
        rows = [_base("binding", 1), _base("event_delta", 9), _base(),
                _base("event_delta", 11)]
        rows[-1]["event"]["amount"] = 1950
        t = 1_800_000_000_000
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "BrainOfCatShadowCheckpoints.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            imported = checkpoint.import_jsonl(path, chronicle_events=iter([
                _chronicle(t - 10), _chronicle(t + 1010, amount=1950, event_index=2)
            ]))
        self.assertEqual(imported["record_count"], 4)
        self.assertEqual(imported["checkpoint_count"], 1)
        self.assertEqual(imported["exact_checkpoint_count"], 0)
        self.assertEqual(imported["exact_chronicle_join_count"], 0)
        self.assertEqual(imported["client_observed_event_bracket_count"], 1)
        self.assertEqual(imported["checkpoints"][0]["chronicle_join"]["status"],
                         "CLIENT_OBSERVED_EVENT_BRACKET")


if __name__ == "__main__":
    unittest.main()
