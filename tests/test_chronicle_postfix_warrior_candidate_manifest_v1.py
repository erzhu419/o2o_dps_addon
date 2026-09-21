from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from o2o_dps.chronicle_postfix_warrior_candidate_manifest_v1 import (
    build_postfix_warrior_candidate_manifest_v1,
)


class FakeClient:
    def __init__(self):
        self.paths = []

    def get_json(self, path, *, params):
        self.paths.append((path, params))
        if path == "/raidlogs/recent":
            return None, {
                "activities": [
                    {"id": "clean", "slug": "clean-slug", "name": "Upper Tower of Karazhan",
                     "started_at": "2026-09-16T12:00:00Z", "uploaded_at": "2026-09-20T12:00:00Z",
                     "guild": {"name": "南北"}},
                    {"id": "old", "slug": "old-slug", "name": "Upper Tower of Karazhan",
                     "started_at": "2026-09-02T12:00:00Z", "uploaded_at": "2026-09-20T11:00:00Z"},
                ],
                "pagination": {"has_more": False},
            }
        if path == "/leaderboards":
            return None, {
                "total_count": 2,
                "entries": [
                    {"log_hashed_slug": "clean-slug", "player_guid": "g1", "player_name": "桃姬儿",
                     "talent_layout": "fury", "dps": 1405.2, "killed_at": "2026-09-16T14:00:00Z"},
                    {"log_hashed_slug": "old-slug", "player_guid": "g2", "player_name": "old",
                     "dps": 1500.0, "killed_at": "2026-09-02T14:00:00Z"},
                ],
            }
        raise AssertionError(path)


class CandidateManifestTests(unittest.TestCase):
    def test_only_clean_recent_raid_is_joined_without_event_requests(self):
        client = FakeClient()
        result = build_postfix_warrior_candidate_manifest_v1(
            client, upload_after="2026-09-19T00:00:00Z"
        )
        self.assertEqual(1, result["raid_count"])
        self.assertEqual("clean", result["raids"][0]["instance_id"])
        self.assertEqual("桃姬儿", result["raids"][0]["fury_players"][0]["name"])
        self.assertEqual("南北", result["raids"][0]["guild_name"])
        self.assertEqual("RANKED_FURY", result["raids"][0]["status"])
        self.assertIn(("upload_after", "2026-09-19T00:00:00Z"), client.paths[0][1])
        self.assertEqual(["/raidlogs/recent", "/leaderboards"], [p for p, _ in client.paths])


if __name__ == "__main__":
    unittest.main()
