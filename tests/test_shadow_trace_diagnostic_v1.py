import unittest

from o2o_dps.shadow_trace_diagnostic_v1 import summarize_shadow_trace_v1


def event(name, time_s, spell=23894, session="a", source="self"):
    return {
        "kind": "event_delta", "sessionId": session, "pullId": "pull",
        "playerGuid": "self", "at": {"getTimeSeconds": time_s},
        "event": {"name": name, "spellId": spell, "sourceGuid": source},
    }


class ShadowTraceDiagnosticV1Test(unittest.TestCase):
    def test_pairs_only_single_pending_client_cast(self):
        rows = [
            event("SPELL_CAST_EVENT", 1), event("SPELL_GO_SELF", 1.3),
            event("SPELL_CAST_EVENT", 2), event("SPELL_CAST_EVENT", 2.1),
            event("SPELL_GO_SELF", 2.4), event("SPELL_GO_SELF", 3),
            event("SPELL_CAST_EVENT", 4), event("SPELL_FAILED_SELF", 4.1),
            event("SPELL_GO_SELF", 4.2, source="other"),
        ]
        result = summarize_shadow_trace_v1(rows)
        bt = next(row for row in result["spells"] if row["spell_id"] == 23894)
        self.assertEqual([300], bt["paired_cast_to_go_ms"])
        self.assertEqual(1, bt["ambiguous_go"])
        self.assertEqual(1, bt["unpaired_go"])
        self.assertEqual(3, bt["observed_go"])

    def test_session_boundary_does_not_pair(self):
        rows = [event("SPELL_CAST_EVENT", 1), event("SPELL_GO_SELF", 1.2, session="b")]
        result = summarize_shadow_trace_v1(rows)
        bt = next(row for row in result["spells"] if row["spell_id"] == 23894)
        self.assertEqual([], bt["paired_cast_to_go_ms"])
        self.assertEqual(1, bt["unpaired_go"])

    def test_partial_visible_health_is_not_exact_checkpoint(self):
        rows = [{"kind": "checkpoint", "fields": {
            "target.current_health": {"quality": "PARTIAL", "value": 80},
            "target.max_health": {"quality": "PARTIAL", "value": 100},
        }}]
        result = summarize_shadow_trace_v1(rows)
        self.assertEqual(1, result["checkpoint_count"])
        self.assertEqual(1, result["partial_target_health_checkpoint_count"])

    def test_heroic_strike_is_not_slam_or_cast_time(self):
        result = summarize_shadow_trace_v1([event("SPELL_CAST_EVENT", 1, spell=25286),
                                             event("SPELL_GO_SELF", 2.8, spell=25286)])
        hs = next(row for row in result["spells"] if row["spell_id"] == 25286)
        self.assertEqual("HEROIC_STRIKE", hs["name"])
        self.assertFalse(hs["cast_time_included"])
        self.assertEqual([1800], hs["paired_cast_to_go_ms"])


if __name__ == "__main__":
    unittest.main()
