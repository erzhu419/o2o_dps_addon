from __future__ import annotations

from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import o2o_dps.chronicle_external_api_ingest_v1 as ingest_module  # noqa: E402
from o2o_dps.chronicle_external_api_ingest_v1 import (  # noqa: E402
    ChronicleClient,
    ChronicleHTTPError,
    ChronicleIngestError,
    ChronicleSafetyLimitError,
    HTTPResponse,
    IMPLEMENTATION_REVISION,
    NO_KNOWN_RULE_MATCH,
    PARSER_CONTRACT_REVISION,
    POSTFIX_KNOWN_CLEAN,
    RANGE_BUG_BOUNDARY_UNCERTAIN_NONVOTING,
    SUSPECT_36YD_RANGE_BUG,
    UNKNOWN_NONVOTING,
    classify_range_bug,
    fetch_recent_pages,
    ingest_external_api,
    list_external_api,
    main,
    replay_manifest_from_local_raw,
    validate_event_gzip,
)


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")


def _response(url: str, value: object, *, status: int = 200) -> HTTPResponse:
    return HTTPResponse(
        status=status,
        headers={"content-type": "application/json"},
        body=_json_bytes(value),
        url=url,
    )


class Router:
    def __init__(self, handler):
        self.handler = handler
        self.calls: list[tuple[str, str, dict[str, str], float]] = []

    def __call__(self, method: str, url: str, headers, timeout: float) -> HTTPResponse:
        copied_headers = {str(key): str(value) for key, value in headers.items()}
        self.calls.append((method, url, copied_headers, timeout))
        return self.handler(method, url, copied_headers, timeout)


def _client(router: Router, *, max_attempts: int = 1) -> ChronicleClient:
    return ChronicleClient(
        transport=router,
        max_attempts=max_attempts,
        request_interval_seconds=0,
    )


def _recent_page(activities: list[dict], *, has_more: bool = False) -> dict:
    return {
        "activities": activities,
        "pagination": {"page": 1, "page_size": 50, "has_more": has_more},
    }


def _activity(
    instance_id: str,
    *,
    slug: str,
    uploaded_at: str,
    started_at: str,
    guild_name: str | None = None,
) -> dict:
    row = {
        "id": instance_id,
        "slug": slug,
        "name": "Upper Tower of Karazhan",
        "uploaded_at": uploaded_at,
        "started_at": started_at,
        "ended_at": started_at,
    }
    if guild_name is not None:
        row["guild_name"] = guild_name
    return row


def _metadata(
    instance_id: str,
    slug: str,
    players: list[dict] | dict[str, dict] | None = None,
    *,
    guild_name: str | None = None,
) -> dict:
    value = {
        "id": instance_id,
        "slug": slug,
        "name": "Upper Tower of Karazhan",
        "players": players if players is not None else [],
        "encounters": [],
    }
    if guild_name is not None:
        value["guild"] = {"id": "guild-id", "name": guild_name}
    return value


class ContaminationContractTests(unittest.TestCase):
    def test_rfc3339_two_digit_fraction_is_cross_version_valid(self) -> None:
        parsed = ingest_module._parse_rfc3339(
            "2026-08-21T11:56:34.88Z", field="started_at"
        )
        self.assertEqual(
            datetime(2026, 8, 21, 11, 56, 34, 880000, tzinfo=timezone.utc),
            parsed,
        )

    def test_exact_five_labels_and_asia_shanghai_three_segment_boundary(self) -> None:
        self.assertEqual(
            classify_range_bug("南北", "2026-09-02T15:59:59Z"),
            SUSPECT_36YD_RANGE_BUG,
        )
        self.assertEqual(
            classify_range_bug("南北", "2026-09-02T16:00:00Z"),
            RANGE_BUG_BOUNDARY_UNCERTAIN_NONVOTING,
        )
        self.assertEqual(
            classify_range_bug("南北", "2026-09-03T03:59:59.999999Z"),
            RANGE_BUG_BOUNDARY_UNCERTAIN_NONVOTING,
        )
        self.assertEqual(
            classify_range_bug("南北", "2026-09-03T04:00:00Z"),
            POSTFIX_KNOWN_CLEAN,
        )
        self.assertEqual(
            classify_range_bug("南北", "2026-09-03T12:48:06.919Z"),
            POSTFIX_KNOWN_CLEAN,
        )
        self.assertEqual(
            classify_range_bug("Another Guild", "2026-08-01T00:00:00Z"),
            NO_KNOWN_RULE_MATCH,
        )
        self.assertEqual(classify_range_bug(None, "2026-09-01T00:00:00Z"), UNKNOWN_NONVOTING)
        self.assertEqual(classify_range_bug("南北", None), UNKNOWN_NONVOTING)
        self.assertEqual(classify_range_bug("南北", "not-a-time"), UNKNOWN_NONVOTING)
        self.assertEqual(
            classify_range_bug("南北", "0001-01-01T00:00:00Z"), UNKNOWN_NONVOTING
        )

    def test_exact_identity_conflict_is_one_nonvoting_observation(self) -> None:
        rows = ingest_module._extract_warrior_observations(
            {
                "players": [
                    {
                        "guid": "Player-1",
                        "name": "Example",
                        "class": "Warrior",
                        "spec": "Fury",
                        "guild_name": "南北",
                    }
                ]
            },
            started_at="2026-09-02T00:00:00+08:00",
            leaderboard_entries=[
                {
                    "player_guid": "player-1",
                    "player_name": "Example",
                    "player_class": "WARRIOR",
                    "player_spec": "Arms",
                    "guild_name": "Other Guild",
                }
            ],
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["player_spec"], "Unknown")
        self.assertEqual(rows[0]["contamination_label"], UNKNOWN_NONVOTING)
        self.assertEqual(rows[0]["spec_evidence_status"], UNKNOWN_NONVOTING)
        self.assertEqual(rows[0]["sources"], ["instance_metadata", "leaderboard_snapshot"])


class ChronicleClientTests(unittest.TestCase):
    def test_no_auth_headers_and_retry_after_is_respected(self) -> None:
        state = {"now": 0.0}
        sleeps: list[float] = []
        attempts = 0

        def sleeper(seconds: float) -> None:
            sleeps.append(seconds)
            state["now"] += seconds

        def handler(method, url, headers, timeout):
            nonlocal attempts
            attempts += 1
            self.assertEqual(method, "GET")
            self.assertNotIn("Authorization", headers)
            self.assertNotIn("Cookie", headers)
            if attempts == 1:
                return HTTPResponse(429, {"Retry-After": "2"}, b"slow", url)
            return _response(url, {"status": "ok"})

        router = Router(handler)
        client = ChronicleClient(
            transport=router,
            max_attempts=2,
            request_interval_seconds=1.05,
            monotonic=lambda: state["now"],
            sleep=sleeper,
            now=lambda: datetime(2026, 9, 11, tzinfo=timezone.utc),
        )

        _, value = client.get_json("/health")

        self.assertEqual(value, {"status": "ok"})
        self.assertEqual(attempts, 2)
        self.assertEqual(sleeps, [2.0])

    def test_fixed_interval_paces_successive_requests(self) -> None:
        state = {"now": 10.0}
        sleeps: list[float] = []

        def sleeper(seconds: float) -> None:
            sleeps.append(seconds)
            state["now"] += seconds

        router = Router(lambda method, url, headers, timeout: _response(url, {}))
        client = ChronicleClient(
            transport=router,
            request_interval_seconds=1.05,
            monotonic=lambda: state["now"],
            sleep=sleeper,
        )
        client.get("/health")
        client.get("/health")
        self.assertEqual(len(sleeps), 1)
        self.assertAlmostEqual(sleeps[0], 1.05)

    def test_nonretryable_status_fails(self) -> None:
        router = Router(
            lambda method, url, headers, timeout: HTTPResponse(400, {}, b"bad", url)
        )
        with self.assertRaises(ChronicleHTTPError):
            _client(router).get("/health")

    def test_https_is_required(self) -> None:
        with self.assertRaises(ChronicleIngestError):
            ChronicleClient(api_base="http://example.invalid")


class EventEnvelopeTests(unittest.TestCase):
    def test_validates_complete_gzip_without_decoding_protobuf(self) -> None:
        compressed = gzip.compress(b"custom-framed-payload", mtime=0)
        self.assertEqual(validate_event_gzip(compressed), len(b"custom-framed-payload"))
        with self.assertRaises(ChronicleIngestError):
            validate_event_gzip(b"not-gzip")
        with self.assertRaises(ChronicleIngestError):
            validate_event_gzip(compressed[:-2])


class RecentListingTests(unittest.TestCase):
    def test_repeated_instance_names_and_upload_after_are_sent(self) -> None:
        def handler(method, url, headers, timeout):
            query = parse_qs(urlparse(url).query)
            self.assertEqual(query["upload_after"], ["2026-09-01T00:00:00Z"])
            self.assertEqual(query["instance_name"], ["A", "B"])
            self.assertNotIn("after_date", query)
            return _response(url, _recent_page([]))

        pages, truncated = fetch_recent_pages(
            _client(Router(handler)),
            upload_after="2026-09-01T00:00:00Z",
            instance_names=["A", "B"],
        )
        self.assertEqual(len(pages), 1)
        self.assertFalse(truncated)

    def test_partial_page_range_is_rejected_for_ingest_but_list_marks_it(self) -> None:
        router = Router(
            lambda method, url, headers, timeout: _response(
                url, _recent_page([], has_more=True)
            )
        )
        client = _client(router)
        with self.assertRaises(ChronicleSafetyLimitError):
            fetch_recent_pages(
                client,
                upload_after="2026-09-01T00:00:00Z",
                max_pages=1,
                require_complete=True,
            )
        result = list_external_api(
            client=client,
            upload_after="2026-09-01T00:00:00Z",
            instance_names=[],
            realm_id=None,
            guild_id=None,
            has_video=None,
            page_size=25,
            max_pages=1,
            leaderboard_queries=[],
        )
        self.assertFalse(result["writes"])
        self.assertTrue(result["recent"]["truncated_by_max_pages"])


class ChronicleIngestTests(unittest.TestCase):
    def test_recent_nested_guild_is_per_instance_in_ingest_and_replay(self) -> None:
        with_guild = _activity(
            "guild-raid",
            slug="guild-slug",
            uploaded_at="2026-09-20T14:17:38.889861Z",
            started_at="2026-09-20T12:18:12.297Z",
        )
        with_guild["guild"] = {"id": "guild-id", "name": "南北"}
        without_guild = _activity(
            "unknown-raid",
            slug="unknown-slug",
            uploaded_at="2026-09-20T13:05:42.008596Z",
            started_at="2026-09-20T11:37:48.309Z",
        )

        def handler(method, url, headers, timeout):
            path = urlparse(url).path
            if path.endswith("/raidlogs/recent"):
                return _response(url, _recent_page([with_guild, without_guild]))
            if path.endswith("/guild-raid"):
                return _response(url, _metadata("guild-raid", "guild-slug"))
            if path.endswith("/unknown-raid"):
                return _response(url, _metadata("unknown-raid", "unknown-slug"))
            self.fail(url)

        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "offline_data"
            result = ingest_external_api(
                data_root=data_root,
                client=_client(Router(handler)),
                upload_after="2026-09-20T00:00:00Z",
                max_instances=2,
            )
            manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
            rows = {row["instance_id"]: row for row in manifest["instances"]}
            self.assertEqual(rows["guild-raid"]["contamination_guild_context"], "南北")
            self.assertEqual(rows["guild-raid"]["contamination_guild_evidence"], "activity.guild.name")
            self.assertEqual(rows["guild-raid"]["uploaded_at"], with_guild["uploaded_at"])
            self.assertEqual(rows["guild-raid"]["started_at"], with_guild["started_at"])
            self.assertIsNone(rows["unknown-raid"]["contamination_guild_context"])
            self.assertEqual(rows["unknown-raid"]["instance_contamination_label"], UNKNOWN_NONVOTING)
            replayed = replay_manifest_from_local_raw(
                Path(result["manifest_path"]), data_root=data_root
            )
            self.assertEqual(replayed["status"], "ALREADY_CURRENT_LOCAL_RAW")

    def test_manifest_last_full_snapshot_and_uploaded_watermark(self) -> None:
        activities = [
            _activity(
                "instance-a",
                slug="slug-a",
                uploaded_at="2026-09-10T02:00:00Z",
                started_at="2026-08-31T15:59:59Z",
            ),
            _activity(
                "instance-b",
                slug="slug-b",
                uploaded_at="2026-09-11T01:00:00Z",
                # Deliberately earlier than A: started_at must not order the cursor.
                started_at="2026-08-01T00:00:00Z",
            ),
        ]
        player_a = {
            "guid": "Player-A",
            "name": "托尼牛",
            "class": "Warrior",
            "spec": "Arms",
            # Leaderboard supplies the exact guild without duplicating this player.
        }
        player_b = {
            "guid": "Player-B",
            "name": "Fury Example",
            "class": "Warrior",
            "spec": "Fury",
            "guild": {"name": "Other Guild"},
        }
        compressed = gzip.compress(b"\x00framed", mtime=0)

        def handler(method, url, headers, timeout):
            parsed = urlparse(url)
            if parsed.path.endswith("/raidlogs/recent"):
                return _response(url, _recent_page(activities))
            if parsed.path.endswith("/leaderboards"):
                return _response(
                    url,
                    {
                        "entries": [
                            {
                                "log_hashed_slug": "slug-a",
                                "player_guid": "Player-A",
                                "player_name": "托尼牛",
                                "player_class": "WARRIOR",
                                "player_spec": "Arms",
                                "guild_name": "南北",
                            }
                        ],
                        "total_count": 1,
                    },
                )
            if parsed.path.endswith("/instance-a"):
                return _response(
                    url,
                    _metadata("instance-a", "slug-a", [player_a], guild_name="南北"),
                )
            if parsed.path.endswith("/instance-b"):
                return _response(
                    url,
                    _metadata(
                        "instance-b", "slug-b", [player_b], guild_name="Other Guild"
                    ),
                )
            if parsed.path.endswith("/ranking-records"):
                return HTTPResponse(200, {}, b"[]", url)
            if parsed.path.endswith("/events/damage"):
                if "/instance-b/" in parsed.path:
                    return HTTPResponse(404, {}, b"missing", url)
                return HTTPResponse(200, {"content-type": "application/octet-stream"}, compressed, url)
            self.fail(f"unexpected URL: {url}")

        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "offline_data"
            writes: list[Path] = []
            real_atomic = ingest_module._atomic_write_bytes
            real_replace = ingest_module._atomic_replace_bytes

            def tracked_atomic(path: Path, data: bytes) -> None:
                writes.append(path)
                real_atomic(path, data)

            def tracked_replace(path: Path, data: bytes) -> None:
                writes.append(path)
                real_replace(path, data)

            with mock.patch.object(ingest_module, "_atomic_write_bytes", tracked_atomic), mock.patch.object(
                ingest_module, "_atomic_replace_bytes", tracked_replace
            ):
                result = ingest_external_api(
                    data_root=data_root,
                    client=_client(Router(handler)),
                    upload_after="2026-09-01T00:00:00Z",
                    instance_names=["Upper Tower of Karazhan"],
                    max_instances=2,
                    stream_types=["damage"],
                    include_ranking_records=True,
                    leaderboard_queries=[
                        {
                            "instance_names": "Upper Tower of Karazhan",
                            "class": "WARRIOR",
                            "limit": 20,
                        }
                    ],
                )

            manifest_path = Path(result["manifest_path"])
            manifest_bytes = manifest_path.read_bytes()
            self.assertEqual(
                manifest_path.stem, hashlib.sha256(manifest_bytes).hexdigest()
            )
            manifest = json.loads(manifest_bytes)
            self.assertEqual(manifest["implementation_revision"], IMPLEMENTATION_REVISION)
            self.assertEqual(
                manifest["parser_contract_revision"], PARSER_CONTRACT_REVISION
            )
            self.assertEqual(manifest["cursor_contract"]["source_field"], "uploaded_at")
            self.assertEqual(
                manifest["cursor_contract"]["started_at_role"],
                "provenance_and_contamination_only_never_cursor",
            )
            self.assertEqual(
                result["watermark"]["upload_after"], "2026-09-11T01:00:00Z"
            )
            self.assertEqual(result["watermark"]["boundary_instance_ids"], ["instance-b"])
            rows = {row["instance_id"]: row for row in manifest["instances"]}
            self.assertEqual(rows["instance-a"]["uploaded_at"], "2026-09-10T02:00:00Z")
            self.assertEqual(rows["instance-a"]["started_at"], "2026-08-31T15:59:59Z")
            self.assertEqual(rows["instance-a"]["started_at_source"], "direct")
            self.assertEqual(
                rows["instance-a"]["started_at_source_field"], "activity.started_at"
            )
            self.assertEqual(rows["instance-a"]["uploaded_at_source"], "direct")
            self.assertEqual(
                rows["instance-a"]["uploaded_at_source_field"], "activity.uploaded_at"
            )
            self.assertEqual(
                rows["instance-a"]["warrior_observations"][0]["contamination_label"],
                SUSPECT_36YD_RANGE_BUG,
            )
            self.assertEqual(rows["instance-a"]["warrior_spec_counts"]["Arms"], 1)
            self.assertEqual(rows["instance-a"]["warrior_spec_counts"]["Fury"], 0)
            self.assertEqual(rows["instance-b"]["warrior_spec_counts"]["Fury"], 1)
            self.assertEqual(
                rows["instance-b"]["warrior_observations"][0]["contamination_label"],
                NO_KNOWN_RULE_MATCH,
            )
            self.assertEqual(rows["instance-a"]["streams"]["damage"]["status"], "AVAILABLE")
            self.assertEqual(rows["instance-b"]["streams"]["damage"]["status"], "UNAVAILABLE_404")

            manifest_write = next(i for i, path in enumerate(writes) if path.parent.name == "manifests")
            watermark_write = next(i for i, path in enumerate(writes) if path.parent.name == "watermarks")
            object_writes = [
                i for i, path in enumerate(writes) if "objects" in path.parts
            ]
            self.assertTrue(object_writes)
            self.assertTrue(all(index < manifest_write for index in object_writes))
            self.assertLess(manifest_write, watermark_write)
            for row in manifest["instances"]:
                refs = [row["metadata"]["object"]]
                if row["ranking_records"]:
                    refs.append(row["ranking_records"]["object"])
                refs.extend(
                    value["object"]
                    for value in row["streams"].values()
                    if value["object"] is not None
                )
                for ref in refs:
                    object_path = data_root / "chronicle_raw" / "external_api" / "v1" / ref["relative_path"]
                    self.assertTrue(object_path.is_file())
                    self.assertIn("offline_data", object_path.parts)

    def test_inclusive_watermark_boundary_deduplicates_without_missing_new_id(self) -> None:
        boundary_time = "2026-09-10T00:00:00Z"
        invocation = {"value": 0}

        def make_router():
            invocation["value"] += 1
            run = invocation["value"]

            def handler(method, url, headers, timeout):
                path = urlparse(url).path
                if path.endswith("/raidlogs/recent"):
                    rows = [_activity("A", slug="a", uploaded_at=boundary_time, started_at=boundary_time)]
                    if run == 2:
                        rows.append(_activity("B", slug="b", uploaded_at=boundary_time, started_at=boundary_time))
                    return _response(url, _recent_page(rows))
                if path.endswith("/A"):
                    return _response(url, _metadata("A", "a"))
                if path.endswith("/B"):
                    return _response(url, _metadata("B", "b"))
                self.fail(url)

            return Router(handler)

        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "offline_data"
            first = ingest_external_api(
                data_root=data_root,
                client=_client(make_router()),
                upload_after="2026-09-01T00:00:00Z",
            )
            self.assertEqual(first["instance_count"], 1)
            second_router = make_router()
            second = ingest_external_api(
                data_root=data_root,
                client=_client(second_router),
                use_watermark=True,
            )
            self.assertEqual(second["instance_count"], 1)
            second_manifest = json.loads(Path(second["manifest_path"]).read_text(encoding="utf-8"))
            self.assertEqual([row["instance_id"] for row in second_manifest["instances"]], ["B"])
            self.assertEqual(second["watermark"]["boundary_instance_ids"], ["A", "B"])
            metadata_paths = [urlparse(call[1]).path for call in second_router.calls]
            self.assertFalse(any(path.endswith("/A") for path in metadata_paths))

    def test_explicit_instance_is_idempotent_and_records_unknown_times(self) -> None:
        router = Router(
            lambda method, url, headers, timeout: _response(
                url, _metadata("selected", "selected-slug")
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "offline_data"
            first = ingest_external_api(
                data_root=data_root,
                client=_client(router),
                explicit_instance_ids=["selected"],
            )
            second = ingest_external_api(
                data_root=data_root,
                client=_client(router),
                explicit_instance_ids=["selected"],
            )
            self.assertEqual(first["manifest_sha256"], second["manifest_sha256"])
            manifest = json.loads(Path(first["manifest_path"]).read_text(encoding="utf-8"))
            row = manifest["instances"][0]
            self.assertIsNone(row["uploaded_at"])
            self.assertIsNone(row["started_at"])
            self.assertEqual(row["instance_contamination_label"], UNKNOWN_NONVOTING)

    def test_explicit_metadata_times_are_preserved_but_do_not_create_a_watermark(self) -> None:
        metadata = _metadata(
            "selected",
            "selected-slug",
            [
                {
                    "guid": "Player-1",
                    "name": "桃姬儿",
                    "class": "Warrior",
                    "spec": "Arms",
                    "guild_name": "南北",
                }
            ],
            guild_name="南北",
        )
        metadata["uploaded_at"] = "2026-09-11T05:00:00Z"
        metadata["started_at"] = "2026-09-03T12:00:00+08:00"
        router = Router(lambda method, url, headers, timeout: _response(url, metadata))
        with tempfile.TemporaryDirectory() as temporary:
            result = ingest_external_api(
                data_root=Path(temporary) / "offline_data",
                client=_client(router),
                explicit_instance_ids=["selected"],
            )
            manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
            row = manifest["instances"][0]
            self.assertEqual(row["uploaded_at"], "2026-09-11T05:00:00Z")
            self.assertEqual(row["started_at"], "2026-09-03T12:00:00+08:00")
            self.assertEqual(row["started_at_source"], "direct")
            self.assertEqual(row["started_at_source_field"], "metadata.started_at")
            self.assertEqual(row["instance_contamination_label"], POSTFIX_KNOWN_CLEAN)
            self.assertIsNone(result["watermark"])

    def test_real_api_schema_mapping_encounters_guild_and_rankings(self) -> None:
        instance_id = "a26a041a-6b7a-4c26-a1cd-5464e59dee7c"
        player_guid = "0x000000000056DEB3"
        metadata = {
            "id": instance_id,
            "name": "Upper Tower of Karazhan",
            "slug": "rQBpXC7EcYAJHfAv",
            "guild": {
                "id": "65a8fe4c-8023-4ed2-bcef-d14d0feacb6b",
                "name": "南北",
            },
            "encounters": [
                {"id": "bad", "start_time": "not-a-time"},
                {"id": "later", "start_time": "2026-09-03T12:49:00.000Z"},
                {"id": "first", "start_time": "2026-09-03T12:48:06.919Z"},
                {"id": "missing"},
            ],
            "players": {
                player_guid: {
                    "name": "托尼牛",
                    "class": "Warrior",
                    "race": "Orc",
                    "level": 60,
                },
                "0x0000000000000002": {
                    "name": "Mage",
                    "class": "Mage",
                    "race": "Human",
                    "level": 60,
                },
            },
        }
        rankings = [
            {
                "encounter_id": "first",
                "player_guid": player_guid,
                "player_name": "托尼牛",
                "player_class": "WARRIOR",
                "player_spec": "Arms",
                "player_role": "dps",
            },
            {
                "encounter_id": "later",
                "player_guid": player_guid,
                "player_name": "托尼牛",
                "player_class": "WARRIOR",
                "player_spec": "Arms",
                "player_role": "dps",
            },
        ]

        def handler(method, url, headers, timeout):
            path = urlparse(url).path
            if path.endswith(f"/{instance_id}"):
                return _response(url, metadata)
            if path.endswith("/ranking-records"):
                return HTTPResponse(200, {}, _json_bytes(rankings), url)
            self.fail(url)

        router = Router(handler)
        with tempfile.TemporaryDirectory() as temporary:
            result = ingest_external_api(
                data_root=Path(temporary) / "offline_data",
                client=_client(router),
                explicit_instance_ids=[instance_id],
                include_ranking_records=True,
            )
            manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
            row = manifest["instances"][0]
            self.assertEqual(row["instance_name"], "Upper Tower of Karazhan")
            self.assertEqual(row["started_at"], "2026-09-03T12:48:06.919Z")
            self.assertEqual(
                row["started_at_source"], "metadata.encounters.min(start_time)"
            )
            self.assertIsNone(row["started_at_source_field"])
            self.assertIsNone(row["uploaded_at"])
            self.assertEqual(row["uploaded_at_source"], "missing")
            self.assertEqual(row["contamination_guild_context"], "南北")
            self.assertEqual(
                row["contamination_guild_evidence"], "metadata.guild.name"
            )
            self.assertEqual(row["instance_contamination_label"], POSTFIX_KNOWN_CLEAN)
            self.assertEqual(row["warrior_spec_counts"], {"Arms": 1, "Fury": 0, "Other_or_unknown": 0})
            self.assertEqual(len(row["warrior_observations"]), 1)
            observation = row["warrior_observations"][0]
            self.assertEqual(observation["player_guid"], player_guid)
            self.assertEqual(observation["player_spec"], "Arms")
            self.assertEqual(
                observation["sources"],
                ["instance_metadata", "instance_ranking_records"],
            )
            self.assertEqual(observation["contamination_guild_context"], "南北")
            self.assertEqual(
                observation["contamination_guild_evidence"], "metadata.guild.name"
            )
            self.assertNotIn("guild_name", observation)
            self.assertEqual(observation["contamination_label"], POSTFIX_KNOWN_CLEAN)
            self.assertEqual(observation["spec_evidence_status"], "OBSERVED")
            self.assertEqual(manifest["implementation_revision"], IMPLEMENTATION_REVISION)
            self.assertEqual(manifest["parser_contract_revision"], PARSER_CONTRACT_REVISION)
            self.assertEqual(result["parser_contract_revision"], PARSER_CONTRACT_REVISION)
            self.assertTrue(urlparse(router.calls[0][1]).path.endswith(f"/{instance_id}"))
            self.assertTrue(urlparse(router.calls[1][1]).path.endswith("/ranking-records"))

    def test_local_raw_replay_rebuilds_legacy_manifest_without_network(self) -> None:
        instance_id = "real-schema-instance"
        player_guid = "Player-Real"
        metadata = {
            "id": instance_id,
            "slug": "real-schema-slug",
            "name": "Upper Tower of Karazhan",
            "guild": {"id": "guild", "name": "南北"},
            "encounters": [
                {"id": "encounter", "start_time": "2026-09-02T00:00:00Z"}
            ],
            "players": {
                player_guid: {
                    "name": "桃姬儿",
                    "class": "Warrior",
                    "race": "Human",
                    "level": 60,
                }
            },
        }
        rankings = [
            {
                "player_guid": player_guid,
                "player_name": "桃姬儿",
                "player_class": "WARRIOR",
                "player_spec": "Arms",
            }
        ]
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "offline_data"
            store = ingest_module.RawObjectStore(data_root)
            metadata_ref = store.put(
                _json_bytes(metadata), suffix="json", media_type="application/json"
            )
            ranking_ref = store.put(
                _json_bytes(rankings), suffix="json", media_type="application/json"
            )
            legacy = {
                "schema": ingest_module.SCHEMA,
                "kind": "chronicle_external_api_raw_snapshot",
                "implementation_revision": (
                    ingest_module.LEGACY_IMPLEMENTATION_REVISION
                ),
                "parser_contract_revision": (
                    ingest_module.LEGACY_PARSER_CONTRACT_REVISION
                ),
                "api_base": ingest_module.EXTERNAL_API_BASE,
                "cursor_contract": {},
                "contamination_contract": {},
                "request": {},
                "recent_pages": [],
                "leaderboard_snapshots": [],
                "instances": [
                    {
                        "instance_id": instance_id,
                        "slug": "real-schema-slug",
                        "instance_name": None,
                        "uploaded_at": None,
                        "started_at": None,
                        "cursor_order_key": [None, instance_id],
                        "metadata": {"object": metadata_ref},
                        "ranking_records": {
                            "object": ranking_ref,
                            "record_count": 1,
                        },
                        "streams": {},
                        "warrior_observations": [],
                        "warrior_spec_counts": {
                            "Arms": 0,
                            "Fury": 0,
                            "Other_or_unknown": 0,
                        },
                    }
                ],
            }
            legacy_bytes = ingest_module._canonical_json_bytes(legacy)
            legacy_hash = hashlib.sha256(legacy_bytes).hexdigest()
            legacy_path = store.root / "manifests" / f"{legacy_hash}.json"
            ingest_module._atomic_write_bytes(legacy_path, legacy_bytes)

            result = replay_manifest_from_local_raw(legacy_path, data_root=data_root)

            self.assertTrue(legacy_path.is_file())
            self.assertEqual(result["status"], "REPLAYED_LOCAL_RAW")
            self.assertEqual(result["network_requests_made"], 0)
            self.assertFalse(result["watermark_mutated"])
            self.assertNotEqual(result["manifest_sha256"], legacy_hash)
            rebuilt = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
            self.assertEqual(rebuilt["implementation_revision"], IMPLEMENTATION_REVISION)
            self.assertEqual(rebuilt["parser_contract_revision"], PARSER_CONTRACT_REVISION)
            self.assertEqual(
                rebuilt["replay_provenance"]["source_manifest_sha256"], legacy_hash
            )
            self.assertEqual(
                rebuilt["replay_provenance"]["source_manifest_adoption_status"],
                "LEGACY_REVISION_MIGRATED_TO_CURRENT",
            )
            row = rebuilt["instances"][0]
            self.assertEqual(row["started_at_source"], "metadata.encounters.min(start_time)")
            self.assertEqual(
                row["instance_contamination_label"], SUSPECT_36YD_RANGE_BUG
            )
            self.assertEqual(row["warrior_spec_counts"]["Arms"], 1)

            fixed_point = replay_manifest_from_local_raw(
                Path(result["manifest_path"]), data_root=data_root
            )
            self.assertEqual(fixed_point["status"], "ALREADY_CURRENT_LOCAL_RAW")
            self.assertEqual(fixed_point["manifest_sha256"], result["manifest_sha256"])
            self.assertEqual(fixed_point["manifest_path"], result["manifest_path"])

            poisoned = json.loads(
                Path(result["manifest_path"]).read_text(encoding="utf-8")
            )
            poisoned["instances"][0]["instance_contamination_label"] = (
                POSTFIX_KNOWN_CLEAN
            )
            poisoned_bytes = ingest_module._canonical_json_bytes(poisoned)
            poisoned_path = store.root / "manifests" / (
                f"{hashlib.sha256(poisoned_bytes).hexdigest()}.json"
            )
            poisoned_path.write_bytes(poisoned_bytes)
            with self.assertRaisesRegex(
                ChronicleIngestError, "semantic projections do not match"
            ):
                replay_manifest_from_local_raw(poisoned_path, data_root=data_root)

    def test_local_raw_replay_rejects_unversioned_noncanonical_and_out_of_store_sources(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "offline_data"
            store = ingest_module.RawObjectStore(data_root)
            manifests = store.root / "manifests"
            manifests.mkdir(parents=True, exist_ok=True)
            base = {
                "schema": ingest_module.SCHEMA,
                "kind": "chronicle_external_api_raw_snapshot",
                "implementation_revision": ingest_module.LEGACY_IMPLEMENTATION_REVISION,
                "parser_contract_revision": ingest_module.LEGACY_PARSER_CONTRACT_REVISION,
                "leaderboard_snapshots": [],
                "instances": [],
            }

            for field in ("implementation_revision", "parser_contract_revision"):
                malformed = dict(base)
                malformed.pop(field)
                payload = ingest_module._canonical_json_bytes(malformed)
                path = manifests / f"{hashlib.sha256(payload).hexdigest()}.json"
                path.write_bytes(payload)
                with self.subTest(missing=field):
                    with self.assertRaisesRegex(
                        ChronicleIngestError, "revision pair is not supported"
                    ):
                        replay_manifest_from_local_raw(path, data_root=data_root)

            unknown = {**base, "implementation_revision": "unknown"}
            payload = ingest_module._canonical_json_bytes(unknown)
            path = manifests / f"{hashlib.sha256(payload).hexdigest()}.json"
            path.write_bytes(payload)
            with self.assertRaisesRegex(
                ChronicleIngestError, "revision pair is not supported"
            ):
                replay_manifest_from_local_raw(path, data_root=data_root)

            wrong_kind = {**base, "kind": "not-a-raw-snapshot"}
            payload = ingest_module._canonical_json_bytes(wrong_kind)
            path = manifests / f"{hashlib.sha256(payload).hexdigest()}.json"
            path.write_bytes(payload)
            with self.assertRaisesRegex(ChronicleIngestError, "kind is not supported"):
                replay_manifest_from_local_raw(path, data_root=data_root)

            noncanonical = json.dumps(base, ensure_ascii=False, indent=2).encode("utf-8")
            path = manifests / f"{hashlib.sha256(noncanonical).hexdigest()}.json"
            path.write_bytes(noncanonical)
            with self.assertRaisesRegex(ChronicleIngestError, "not canonical JSON"):
                replay_manifest_from_local_raw(path, data_root=data_root)

            canonical = ingest_module._canonical_json_bytes(base)
            outside = data_root / f"{hashlib.sha256(canonical).hexdigest()}.json"
            outside.write_bytes(canonical)
            with self.assertRaisesRegex(
                ChronicleIngestError, "direct child.*raw manifests directory"
            ):
                replay_manifest_from_local_raw(outside, data_root=data_root)

    def test_recent_uploaded_at_before_cursor_is_rejected_before_publication(self) -> None:
        router = Router(
            lambda method, url, headers, timeout: _response(
                url,
                _recent_page(
                    [
                        _activity(
                            "A",
                            slug="a",
                            uploaded_at="2026-08-31T23:59:59Z",
                            started_at="2026-08-01T00:00:00Z",
                        )
                    ]
                ),
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "offline_data"
            with self.assertRaises(ChronicleIngestError):
                ingest_external_api(
                    data_root=data_root,
                    client=_client(router),
                    upload_after="2026-09-01T00:00:00Z",
                )
            self.assertFalse(data_root.exists())

    def test_failed_stream_does_not_publish_manifest_or_watermark_and_can_resume(self) -> None:
        compressed = gzip.compress(b"valid", mtime=0)
        valid = {"value": False}

        def handler(method, url, headers, timeout):
            path = urlparse(url).path
            if path.endswith("/raidlogs/recent"):
                return _response(
                    url,
                    _recent_page(
                        [_activity("A", slug="a", uploaded_at="2026-09-10T00:00:00Z", started_at="2026-09-09T00:00:00Z")]
                    ),
                )
            if path.endswith("/A"):
                return _response(url, _metadata("A", "a"))
            if path.endswith("/events/damage"):
                return HTTPResponse(200, {}, compressed if valid["value"] else b"bad", url)
            self.fail(url)

        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "offline_data"
            with self.assertRaises(ChronicleIngestError):
                ingest_external_api(
                    data_root=data_root,
                    client=_client(Router(handler)),
                    upload_after="2026-09-01T00:00:00Z",
                    stream_types=["damage"],
                )
            raw_root = data_root / "chronicle_raw" / "external_api" / "v1"
            self.assertFalse((raw_root / "manifests").exists())
            self.assertFalse((raw_root / "watermarks").exists())
            valid["value"] = True
            result = ingest_external_api(
                data_root=data_root,
                client=_client(Router(handler)),
                upload_after="2026-09-01T00:00:00Z",
                stream_types=["damage"],
            )
            self.assertEqual(result["status"], "INGESTED")

    def test_safety_limits_prevent_partial_or_unbounded_ingest(self) -> None:
        router = Router(
            lambda method, url, headers, timeout: _response(
                url,
                _recent_page(
                    [
                        _activity("A", slug="a", uploaded_at="2026-09-10T00:00:00Z", started_at="2026-09-10T00:00:00Z"),
                        _activity("B", slug="b", uploaded_at="2026-09-10T01:00:00Z", started_at="2026-09-10T01:00:00Z"),
                    ]
                ),
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "offline_data"
            with self.assertRaises(ChronicleSafetyLimitError):
                ingest_external_api(
                    data_root=data_root,
                    client=_client(router),
                    upload_after="2026-09-01T00:00:00Z",
                    max_instances=1,
                )
            self.assertFalse(data_root.exists())
            with self.assertRaises(ChronicleSafetyLimitError):
                ingest_external_api(data_root=data_root, client=_client(router))

    def test_raw_root_must_be_offline_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ChronicleIngestError):
                ingest_external_api(
                    data_root=Path(temporary) / "raw",
                    client=_client(Router(lambda *args: self.fail("network must not run"))),
                    explicit_instance_ids=["A"],
                )

    def test_leaderboard_only_snapshot_is_bounded_and_published(self) -> None:
        def handler(method, url, headers, timeout):
            query = parse_qs(urlparse(url).query)
            self.assertEqual(query["limit"], ["20"])
            return _response(url, {"entries": [], "total_count": 0})

        with tempfile.TemporaryDirectory() as temporary:
            result = ingest_external_api(
                data_root=Path(temporary) / "offline_data",
                client=_client(Router(handler)),
                leaderboard_queries=[{"class": "WARRIOR", "spec": "Fury", "limit": 20}],
            )
            self.assertEqual(result["instance_count"], 0)
            manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["leaderboard_snapshots"]), 1)
            self.assertIsNone(result["watermark"])


class CLITests(unittest.TestCase):
    def test_dry_run_makes_no_http_or_filesystem_writes(self) -> None:
        output = io.StringIO()
        with mock.patch.object(ingest_module, "_make_client") as make_client:
            status = main(
                [
                    "dry-run",
                    "--upload-after",
                    "2026-09-01T00:00:00Z",
                    "--instance-name",
                    "Upper Tower of Karazhan",
                    "--stream",
                    "damage",
                    "--leaderboard",
                ],
                stdout=output,
            )
        self.assertEqual(status, 0)
        make_client.assert_not_called()
        value = json.loads(output.getvalue())
        self.assertEqual(value["network_requests_made"], 0)
        self.assertFalse(value["writes"])
        self.assertEqual(value["stream_types"], ["damage"])

    def test_list_without_bounded_selector_is_rejected(self) -> None:
        with self.assertRaises(ChronicleSafetyLimitError):
            main(["list"], stdout=io.StringIO())


if __name__ == "__main__":
    unittest.main()
