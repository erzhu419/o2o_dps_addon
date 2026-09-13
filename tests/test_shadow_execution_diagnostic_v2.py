import unittest

from o2o_dps.shadow_execution_diagnostic_v2 import (
    parse_nampower_casts_v2, summarize_shadow_execution_v2,
)


def event(name, spell, milliseconds, ordinal, *, amount=None, source=None):
    payload = {"name": name, "spellId": spell}
    if source is not None:
        payload["sourceGuid"] = source
    if amount is not None:
        payload["amount"] = amount
    return {
        "kind": "event_delta", "sessionId": "s", "pullId": "p", "playerGuid": "self",
        "at": {"getTimeSeconds": milliseconds / 1000, "causalOrdinal": ordinal},
        "event": payload,
    }


class ShadowExecutionDiagnosticV2Test(unittest.TestCase):
    def test_result_before_go_and_spell_identity(self):
        rows = [
            event("SPELL_CAST_EVENT", 23894, 1000, 1),
            event("SPELL_START_SELF", 23894, 1300, 2, source="self"),
            event("SPELL_MISS_SELF", 23894, 1301, 3, source="self"),
            event("SPELL_GO_SELF", 23894, 1302, 4, source="self"),
            event("SPELL_CAST_EVENT", 25286, 2000, 5),
            event("SPELL_START_SELF", 25286, 2200, 6, source="self"),
            event("SPELL_GO_SELF", 25286, 3700, 7, source="self"),
            event("SPELL_DAMAGE_EVENT_SELF", 25286, 3702, 8, amount=500, source="self"),
        ]
        result = summarize_shadow_execution_v2(iter(rows))
        bt, hs = result["executions"]
        self.assertEqual("MISS", bt["result_kinds"][0]["kind"])
        self.assertEqual(302, bt["client_cast_to_go_ms"])
        self.assertEqual("HEROIC_STRIKE", hs["spell_name"])
        self.assertEqual(500, hs["observed_damage"])
        self.assertEqual(1500, hs["start_to_go_ms"])
        self.assertEqual(8, result["event_delta_count"])

    def test_repeated_cast_does_not_make_timing_sample_but_go_result_still_counts(self):
        rows = [
            event("SPELL_CAST_EVENT", 1680, 1000, 1),
            event("SPELL_CAST_EVENT", 1680, 1200, 2),
            event("SPELL_GO_SELF", 1680, 1500, 3, source="self"),
            event("SPELL_DAMAGE_EVENT_SELF", 1680, 1502, 4, amount=400, source="self"),
        ]
        result = summarize_shadow_execution_v2(rows, nampower_casts=[{
            "spell_id": 1680, "begin_to_result_ms": 500, "native_cast_time_ms": 0,
            "cast_number": 7, "status": 0, "result_code": 0,
        }])
        row = result["executions"][0]
        self.assertEqual(2, row["client_cast_count_before_go"])
        self.assertIsNone(row["client_cast_to_go_ms"])
        self.assertEqual(400, row["observed_damage"])
        self.assertIsNone(row["nampower"]["duration_agrees_with_single_client_cast"])

    def test_native_cast_number_pairs_without_absolute_clock_join(self):
        lines = [
            "[DEBUG]2026-09-13 15:56:40.150: BeginCast #4 英勇打击(25286) cast time: 0 buffer: 55 NO Gcd",
            "[DEBUG]2026-09-13 15:56:41.970: Cast result for #4 英勇打击(25286) status 0 result 0 latency 1648",
        ]
        self.assertEqual([{
            "cast_number": 4, "spell_id": 25286, "native_cast_time_ms": 0,
            "begin_to_result_ms": 1820, "status": 0, "result_code": 0,
        }], parse_nampower_casts_v2(lines))


if __name__ == "__main__":
    unittest.main()
