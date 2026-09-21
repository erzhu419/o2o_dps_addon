"""Small, separate development inventory of post-fix Upper Kara Fury logs.

This only reads Chronicle's recent-upload and leaderboard JSON endpoints. It
does not fetch raid event streams or change the frozen old50 evaluation set.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from o2o_dps.chronicle_external_api_ingest_v1 import (
    ChronicleClient,
    ChronicleIngestError,
    ChronicleSafetyLimitError,
    RANGE_BUG_POSTFIX_AT_OR_AFTER_LOCAL,
    _parse_rfc3339,
    fetch_recent_pages,
)


INSTANCE_NAME = "Upper Tower of Karazhan"


def _timestamp(value: str) -> datetime:
    return _parse_rfc3339(value, field="candidate timestamp")


def build_postfix_warrior_candidate_manifest_v1(
    client: ChronicleClient,
    *,
    upload_after: str,
    page_size: int = 50,
    max_pages: int = 4,
    leaderboard_page_size: int = 200,
    max_leaderboard_pages: int = 4,
) -> dict[str, Any]:
    """Return a bounded metadata inventory; never advance an ingest watermark."""
    recent_pages, _ = fetch_recent_pages(
        client,
        upload_after=upload_after,
        instance_names=(INSTANCE_NAME,),
        page_size=page_size,
        max_pages=max_pages,
        require_complete=True,
    )
    cutoff = _timestamp(RANGE_BUG_POSTFIX_AT_OR_AFTER_LOCAL)
    raids: dict[str, dict[str, Any]] = {}
    for _, page in recent_pages:
        for activity in page["activities"]:
            if activity.get("name") != INSTANCE_NAME:
                continue
            started_at = activity.get("started_at")
            if not isinstance(started_at, str) or _timestamp(started_at) < cutoff:
                continue
            raid_id, slug = activity.get("id"), activity.get("slug")
            if not isinstance(raid_id, str) or not isinstance(slug, str):
                raise ChronicleIngestError("recent raid lacks id or slug")
            raids[raid_id] = {
                "instance_id": raid_id,
                "slug": slug,
                "started_at": started_at,
                "uploaded_at": activity.get("uploaded_at"),
                "guild_name": (activity.get("guild") or {}).get("name"),
                "fury_players": [],
            }

    by_slug = {raid["slug"]: raid for raid in raids.values()}
    leaderboard_total = None
    leaderboard_seen = 0
    for page_index in range(max_leaderboard_pages):
        _, payload = client.get_json(
            "/leaderboards",
            params={
                "instance_names": INSTANCE_NAME,
                "class": "WARRIOR",
                "spec": "Fury",
                "role": "dps",
                "metric": "dps",
                "limit": leaderboard_page_size,
                "offset": page_index * leaderboard_page_size,
            },
        )
        entries = payload.get("entries")
        total = payload.get("total_count")
        if not isinstance(entries, list) or not isinstance(total, int):
            raise ChronicleIngestError("leaderboard lacks entries or total_count")
        leaderboard_total = total
        leaderboard_seen += len(entries)
        for entry in entries:
            raid = by_slug.get(entry.get("log_hashed_slug"))
            if raid is None:
                continue
            raid["fury_players"].append(
                {
                    "guid": entry.get("player_guid"),
                    "name": entry.get("player_name"),
                    "talent_layout": entry.get("talent_layout"),
                    "dps": entry.get("dps"),
                    "killed_at": entry.get("killed_at"),
                }
            )
        if leaderboard_seen >= total:
            break
    if leaderboard_total is None or leaderboard_seen < leaderboard_total:
        raise ChronicleSafetyLimitError("leaderboard exceeds max_leaderboard_pages")

    ordered = sorted(raids.values(), key=lambda row: (row["uploaded_at"], row["instance_id"]))
    for raid in ordered:
        raid["fury_players"].sort(key=lambda row: (str(row["guid"]), str(row["killed_at"])))
        raid["status"] = "RANKED_FURY" if raid["fury_players"] else "FURY_NOT_ON_CURRENT_BOARD"
    return {
        "schema": "chronicle_postfix_warrior_candidate_manifest/v1",
        "cohort": "separate_development_postfix_incremental",
        "instance_name": INSTANCE_NAME,
        "upload_after": upload_after,
        "started_at_not_before_local": RANGE_BUG_POSTFIX_AT_OR_AFTER_LOCAL,
        "recent_page_count": len(recent_pages),
        "leaderboard_fury_total_count": leaderboard_total,
        "raid_count": len(ordered),
        "ranked_fury_raid_count": sum(bool(raid["fury_players"]) for raid in ordered),
        "raids": ordered,
    }
