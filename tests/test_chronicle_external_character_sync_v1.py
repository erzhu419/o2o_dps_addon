from __future__ import annotations

from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, unquote, urlparse

from o2o_dps import chronicle_external_api_ingest_v1 as ingest_v1
from o2o_dps import chronicle_external_character_history_v1 as history_v1
from o2o_dps import chronicle_external_character_sync_v1 as sync_v1
from o2o_dps import chronicle_external_team_timeline_v2 as timeline_v2


GUID_TONY = "0x000000000056DEB3"
GUID_TAO = "0x00000000007395BF"
INSTANCE_A = "11111111-1111-1111-1111-111111111111"
INSTANCE_B = "22222222-2222-2222-2222-222222222222"
INSTANCE_C = "33333333-3333-3333-3333-333333333333"
INSTANCE_D = "44444444-4444-4444-4444-444444444444"
INSTANCE_E = "55555555-5555-5555-5555-555555555555"


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _response(url: str, value: object) -> ingest_v1.HTTPResponse:
    return ingest_v1.HTTPResponse(
        status=200,
        headers={"content-type": "application/json"},
        body=_json_bytes(value),
        url=url,
    )


def _identity(name: str, guid: str) -> dict[str, object]:
    return {
        "guid": guid,
        "name": name,
        "class": "Warrior",
        "race": "Tauren",
        "gender": "Male",
        "level": 60,
        "spec": "Arms",
        "role": "dps",
        "item_level": 80.0,
        "server": {"id": "server-capy", "name": "Capybara"},
        "realm": {
            "id": "realm-basin",
            "server_id": "server-capy",
            "name": "Basin of Stars",
        },
        "guild": {"id": "guild-nanbei", "name": "南北"},
        "updated_at": "2026-09-09T15:00:00Z",
    }


def _log(
    instance_id: str,
    *,
    slug: str,
    started_at: str,
    uploaded_at: str,
) -> dict[str, object]:
    return {
        "id": instance_id,
        "slug": slug,
        "name": "Upper Tower of Karazhan",
        "guild": {"id": "guild-nanbei", "name": "南北"},
        "boss_kills": 9,
        "started_at": started_at,
        "ended_at": started_at,
        "uploaded_at": uploaded_at,
        "performance": [
            {
                "encounter_name": "Mephistroth",
                "dps_parse": {"display_score": 99},
                "hps_parse": None,
            }
        ],
    }


class ChronicleFixture:
    def __init__(self) -> None:
        self.identities = {
            "托尼牛": _identity("托尼牛", GUID_TONY),
            "桃姬儿": _identity("桃姬儿", GUID_TAO),
        }
        self.log_a = _log(
            INSTANCE_A,
            slug="slug-a",
            started_at="2026-09-03T12:48:06.919Z",
            uploaded_at="2026-09-03T14:39:03.000507Z",
        )
        self.log_b = _log(
            INSTANCE_B,
            slug="slug-b",
            started_at="2026-08-30T04:50:53Z",
            uploaded_at="2026-09-03T01:00:00Z",
        )
        self.pages = {
            "托尼牛": [[self.log_a], [self.log_b]],
            "桃姬儿": [[deepcopy(self.log_a)]],
        }
        self.urls: list[str] = []
        self.metadata_requests: list[str] = []
        self.ranking_requests: list[str] = []
        self.event_requests: list[tuple[str, str]] = []

    def _activity(self, row: dict[str, object]) -> dict[str, object]:
        return {
            "id": row["id"],
            "slug": row["slug"],
            "name": row["name"],
            "guild_name": "南北",
            "started_at": row["started_at"],
            "uploaded_at": row["uploaded_at"],
        }

    def _metadata(self, instance_id: str) -> dict[str, object]:
        row = self.log_a if instance_id == INSTANCE_A else self.log_b
        return {
            "id": instance_id,
            "slug": row["slug"],
            "name": row["name"],
            "guild": {"id": "guild-nanbei", "name": "南北"},
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "uploaded_at": row["uploaded_at"],
            "players": [],
        }

    def _rankings(self, instance_id: str) -> list[dict[str, object]]:
        players = [(GUID_TONY, "wrong-tony-display")]
        if instance_id == INSTANCE_A:
            players.append((GUID_TAO, "wrong-tao-display"))
        return [
            {
                "id": f"rank-{instance_id}-{index}",
                "encounter_id": "encounter-1",
                "encounter_name": "Mephistroth",
                "player_guid": guid,
                "player_name": display,
                "player_class": "WARRIOR",
                "player_spec": "Arms",
                "player_role": "dps",
                "killed_at": "2026-09-02T14:00:00Z",
                "damage_done": 120000 + index,
                "duration_secs": 100.0,
                "dps": 1200.0 + index / 100.0,
            }
            for index, (guid, display) in enumerate(players)
        ]

    def __call__(self, method, url, headers, timeout):
        self.urls.append(url)
        parsed = urlparse(url)
        path = parsed.path
        parts = [unquote(value) for value in path.split("/")]
        if path.endswith("/raidlogs/recent"):
            query = parse_qs(parsed.query)
            self.assert_recent_query(query)
            cursor = ingest_v1._parse_rfc3339(
                query["upload_after"][0], field="upload_after"
            )
            rows = [self.log_a, self.log_b]
            rows = [
                row
                for row in rows
                if ingest_v1._parse_rfc3339(
                    row["uploaded_at"], field="uploaded_at"
                )
                >= cursor
            ]
            rows.sort(key=lambda row: (row["uploaded_at"], row["id"]))
            page = int(query["page"][0])
            page_size = int(query["page_size"][0])
            start = (page - 1) * page_size
            selected = rows[start : start + page_size]
            return _response(
                url,
                {
                    "activities": [self._activity(row) for row in selected],
                    "pagination": {
                        "page": page,
                        "page_size": page_size,
                        "has_more": start + page_size < len(rows),
                    },
                },
            )
        if "/characters/" in path:
            character = parts[-2] if parts[-1] == "instances" else parts[-1]
            identity = self.identities[character]
            if parts[-1] != "instances":
                return _response(url, identity)
            query = parse_qs(parsed.query)
            page = int(query["page"][0])
            page_size = int(query["page_size"][0])
            character_pages = self.pages[character]
            logs = character_pages[page - 1]
            return _response(
                url,
                {
                    "character": deepcopy(identity),
                    "logs": deepcopy(logs),
                    "pagination": {
                        "page": page,
                        "page_size": page_size,
                        "has_more": page < len(character_pages),
                    },
                },
            )
        if "/raidlogs/instances/" in path:
            instance_id = parts[parts.index("instances") + 1]
            if parts[-1] == "ranking-records":
                self.ranking_requests.append(instance_id)
                return _response(url, self._rankings(instance_id))
            if "events" in parts:
                stream = parts[-1]
                self.event_requests.append((instance_id, stream))
                return ingest_v1.HTTPResponse(
                    status=200,
                    headers={"content-type": "application/octet-stream"},
                    body=gzip.compress(f"{instance_id}:{stream}".encode(), mtime=0),
                    url=url,
                )
            self.metadata_requests.append(instance_id)
            return _response(url, self._metadata(instance_id))
        if path.endswith("/leaderboards"):
            raise AssertionError("character sync must never call leaderboards")
        raise AssertionError(f"unexpected request: {url}")

    def assert_recent_query(self, query: dict[str, list[str]]) -> None:
        if "upload_after" not in query or "uploaded_after" in query:
            raise AssertionError(f"wrong recent cursor query: {query}")


def _client(fixture) -> ingest_v1.ChronicleClient:
    return ingest_v1.ChronicleClient(
        transport=fixture,
        max_attempts=1,
        request_interval_seconds=0,
    )


def _query(name: str) -> history_v1.CharacterQuery:
    return history_v1.CharacterQuery(
        server="Capybara", realm="Basin of Stars", character=name
    )


def _missing_row(
    instance_id: str,
    *,
    player: str,
    label: str,
    metadata: bool,
    ranking: bool,
    streams: list[str],
    ranking_fetch_required: bool | None = None,
    stream_fetch_required: list[str] | None = None,
    terminal_streams: list[str] | None = None,
) -> dict[str, object]:
    return {
        "instance_id": instance_id,
        "character_name": player,
        "contamination_label": label,
        "missing_plan": {
            "metadata": metadata,
            "ranking_exact_dps": ranking,
            "ranking_fetch_required": (
                ranking
                if ranking_fetch_required is None
                else ranking_fetch_required
            ),
            "required_streams": streams,
            "required_streams_fetch_required": (
                streams if stream_fetch_required is None else stream_fetch_required
            ),
            "required_streams_terminal_unavailable_404": terminal_streams or [],
        },
    }


def _warrior_observation(name: str, guid: str) -> dict[str, object]:
    return {
        "player_class": "Warrior",
        "player_guid": guid,
        "player_name": name,
    }


def _write_timeline_manifest(
    data_root: Path,
    observations: list[dict[str, object]],
) -> tuple[Path, dict[str, object], bytes]:
    output = (
        data_root
        / "derived"
        / "chronicle_external_team_timeline"
        / "v2"
        / "fixture"
    )
    output.mkdir(parents=True)
    instance = timeline_v2._content_addressed(
        {
            "instance_id": INSTANCE_A,
            "instance_provenance": {
                "warrior_spec_evidence": {
                    "observation_count": len(observations),
                    "observations": observations,
                }
            },
        }
    )
    manifest = timeline_v2._content_addressed(
        {
            "schema": timeline_v2.SCHEMA,
            "kind": timeline_v2.KIND,
            "implementation_revision": timeline_v2.IMPLEMENTATION_REVISION,
            "status": timeline_v2.STATUS,
            "instance_order": [INSTANCE_A],
            "instances": [instance],
            "summary": {
                "warrior_spec_observation_count": len(observations),
            },
        }
    )
    payload = timeline_v2._canonical_bytes(manifest)
    stable = output / "manifest.json"
    addressed = output / (
        "chronicle_external_team_timeline_v2."
        f"{manifest['content_address']['sha256']}.manifest.json"
    )
    stable.write_bytes(payload)
    addressed.write_bytes(payload)
    return stable, manifest, payload


class ChronicleExternalCharacterSyncV1Tests(unittest.TestCase):
    def test_recent_probe_uses_upload_after_and_deduplicates_only_known_boundary_id(
        self,
    ) -> None:
        cursor = "2026-09-10T15:03:37.108769Z"
        rows = [
            {
                "id": INSTANCE_A,
                "slug": "slug-a",
                "name": "Upper Tower of Karazhan",
                "started_at": "2026-09-10T13:00:00Z",
                "uploaded_at": cursor,
            },
            {
                "id": INSTANCE_C,
                "slug": "slug-c",
                "name": "Upper Tower of Karazhan",
                "started_at": "2026-09-10T13:01:00Z",
                "uploaded_at": cursor,
            },
        ]
        urls: list[str] = []

        def handler(method, url, headers, timeout):
            urls.append(url)
            parsed = urlparse(url)
            query = parse_qs(parsed.query)
            self.assertIn("upload_after", query)
            self.assertNotIn("uploaded_after", query)
            page = int(query["page"][0])
            page_size = int(query["page_size"][0])
            start = (page - 1) * page_size
            return _response(
                url,
                {
                    "activities": rows[start : start + page_size],
                    "pagination": {
                        "page": page,
                        "page_size": page_size,
                        "has_more": start + page_size < len(rows),
                    },
                },
            )

        result = sync_v1.probe_recent_uploads(
            client=_client(handler),
            upload_after=cursor,
            known_boundary_instance_ids=[INSTANCE_A],
            instance_names=["Upper Tower of Karazhan"],
            page_size=1,
            max_pages=2,
        )
        self.assertEqual([INSTANCE_A], result["inclusive_boundary_duplicate_ids"])
        self.assertEqual([INSTANCE_C], result["new_activity_ids"])
        self.assertEqual(2, result["page_count"])
        self.assertTrue(all("upload_after=" in url for url in urls))

    def test_gap_batches_deduplicate_shared_raid_and_hard_gate_nontraining_streams(
        self,
    ) -> None:
        inventory = {
            "schema": history_v1.INVENTORY_SCHEMA,
            "implementation_revision": history_v1.INVENTORY_IMPLEMENTATION_REVISION,
            "instances": [
                _missing_row(
                    INSTANCE_A,
                    player="托尼牛",
                    label=ingest_v1.POSTFIX_KNOWN_CLEAN,
                    metadata=False,
                    ranking=False,
                    streams=["damage"],
                ),
                _missing_row(
                    INSTANCE_A,
                    player="桃姬儿",
                    label=ingest_v1.POSTFIX_KNOWN_CLEAN,
                    metadata=False,
                    ranking=True,
                    streams=["spell_go"],
                ),
                _missing_row(
                    INSTANCE_B,
                    player="托尼牛",
                    label=ingest_v1.SUSPECT_36YD_RANGE_BUG,
                    metadata=True,
                    ranking=True,
                    streams=["damage", "spell_go"],
                ),
                _missing_row(
                    INSTANCE_C,
                    player="桃姬儿",
                    label=ingest_v1.UNKNOWN_NONVOTING,
                    metadata=False,
                    ranking=False,
                    streams=["damage"],
                ),
            ],
        }
        batches = sync_v1.build_gap_batches(inventory)
        by_lane = {batch.lane: batch for batch in batches}
        training = by_lane["TRAINING_ELIGIBLE"]
        self.assertEqual((INSTANCE_A,), training.instance_ids)
        self.assertEqual(("damage", "spell_go"), training.stream_types)
        self.assertTrue(training.include_ranking_records)
        diagnostic = by_lane["NONTRAINING_DIAGNOSTIC"]
        self.assertEqual((INSTANCE_B,), diagnostic.instance_ids)
        self.assertEqual((), diagnostic.stream_types)
        self.assertTrue(diagnostic.include_ranking_records)
        self.assertNotIn(
            INSTANCE_C, {item for batch in batches for item in batch.instance_ids}
        )
        self.assertEqual([], sync_v1.build_gap_batches(inventory, evidence_tier="history"))
        ranking_batches = sync_v1.build_gap_batches(
            inventory, evidence_tier="ranking"
        )
        self.assertTrue(ranking_batches)
        self.assertTrue(all(batch.lane == "RANKING_INDEX" for batch in ranking_batches))
        self.assertTrue(all(not batch.stream_types for batch in ranking_batches))
        self.assertEqual(
            {INSTANCE_A, INSTANCE_B},
            {
                instance_id
                for batch in ranking_batches
                for instance_id in batch.instance_ids
            },
        )

    def test_ranking_batches_close_three_captured_absences_but_fetch_no_object(
        self,
    ) -> None:
        inventory = {
            "schema": history_v1.INVENTORY_SCHEMA,
            "implementation_revision": history_v1.INVENTORY_IMPLEMENTATION_REVISION,
            "instances": [
                *[
                    _missing_row(
                        instance_id,
                        player=f"captured-absent-{index}",
                        label=ingest_v1.POSTFIX_KNOWN_CLEAN,
                        metadata=False,
                        ranking=True,
                        ranking_fetch_required=False,
                        streams=[],
                    )
                    for index, instance_id in enumerate(
                        (INSTANCE_A, INSTANCE_B, INSTANCE_C), start=1
                    )
                ],
                _missing_row(
                    INSTANCE_D,
                    player="endpoint-not-captured",
                    label=ingest_v1.POSTFIX_KNOWN_CLEAN,
                    metadata=False,
                    ranking=True,
                    ranking_fetch_required=True,
                    streams=[],
                ),
                _missing_row(
                    INSTANCE_E,
                    player="exact-ranking-present",
                    label=ingest_v1.POSTFIX_KNOWN_CLEAN,
                    metadata=False,
                    ranking=False,
                    ranking_fetch_required=False,
                    streams=[],
                ),
            ],
        }
        batches = sync_v1.build_gap_batches(inventory, evidence_tier="ranking")
        self.assertEqual(1, len(batches))
        self.assertEqual((INSTANCE_D,), batches[0].instance_ids)
        self.assertTrue(batches[0].include_ranking_records)
        self.assertEqual((), batches[0].stream_types)

    def test_stream_batches_fetch_only_uncaptured_not_terminal_404(self) -> None:
        inventory = {
            "schema": history_v1.INVENTORY_SCHEMA,
            "implementation_revision": history_v1.INVENTORY_IMPLEMENTATION_REVISION,
            "instances": [
                _missing_row(
                    INSTANCE_A,
                    player="terminal-404",
                    label=ingest_v1.POSTFIX_KNOWN_CLEAN,
                    metadata=False,
                    ranking=False,
                    streams=["spell_go"],
                    stream_fetch_required=[],
                    terminal_streams=["spell_go"],
                ),
                _missing_row(
                    INSTANCE_B,
                    player="uncaptured",
                    label=ingest_v1.POSTFIX_KNOWN_CLEAN,
                    metadata=False,
                    ranking=False,
                    streams=["damage"],
                    stream_fetch_required=["damage"],
                ),
            ],
        }
        batches = sync_v1.build_gap_batches(
            inventory, evidence_tier="eligible-streams"
        )
        self.assertEqual(1, len(batches))
        self.assertEqual((INSTANCE_B,), batches[0].instance_ids)
        self.assertEqual(("damage",), batches[0].stream_types)

    def test_stream_batch_conflicting_terminal_and_fetch_states_fail_closed(self) -> None:
        inventory = {
            "schema": history_v1.INVENTORY_SCHEMA,
            "implementation_revision": history_v1.INVENTORY_IMPLEMENTATION_REVISION,
            "instances": [
                _missing_row(
                    INSTANCE_A,
                    player="first-membership",
                    label=ingest_v1.POSTFIX_KNOWN_CLEAN,
                    metadata=False,
                    ranking=False,
                    streams=["damage"],
                    stream_fetch_required=["damage"],
                ),
                _missing_row(
                    INSTANCE_A,
                    player="second-membership",
                    label=ingest_v1.POSTFIX_KNOWN_CLEAN,
                    metadata=False,
                    ranking=False,
                    streams=["damage"],
                    stream_fetch_required=[],
                    terminal_streams=["damage"],
                ),
            ],
        }
        with self.assertRaisesRegex(
            sync_v1.CharacterSyncError, "conflicting stream capture states"
        ):
            sync_v1.build_gap_batches(
                inventory, evidence_tier="eligible-streams"
            )

    def test_timeline_warriors_are_guid_deduplicated_sorted_and_auditable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "offline_data"
            manifest_path, manifest, payload = _write_timeline_manifest(
                data_root,
                [
                    _warrior_observation("托尼牛", GUID_TONY),
                    _warrior_observation("桃姬儿", GUID_TAO),
                    _warrior_observation("托尼牛", GUID_TONY),
                ],
            )
            resolved = sync_v1.resolve_character_queries(
                [_query("托尼牛"), _query("额外战士")],
                players_from_team_timeline_manifest=manifest_path,
                default_server="Capybara",
                default_realm="Basin of Stars",
            )
            names = [query.character for query in resolved.queries]
            self.assertEqual(
                sorted(names, key=lambda value: (value.casefold(), value)), names
            )
            self.assertEqual(3, len(resolved.queries))
            self.assertEqual(2, len(resolved.expected_guids))
            self.assertEqual(GUID_TONY, resolved.expected_guids[_query("托尼牛")])
            self.assertEqual(GUID_TAO, resolved.expected_guids[_query("桃姬儿")])
            source = resolved.source
            self.assertEqual(1, source["manual_timeline_exact_overlap_count"])
            self.assertEqual(3, source["merged_character_query_count"])
            timeline = source["timeline"]
            self.assertEqual(3, timeline["warrior_observation_count"])
            self.assertEqual(2, timeline["unique_warrior_guid_count"])
            self.assertEqual(2, timeline["character_query_count"])
            self.assertEqual(
                hashlib.sha256(payload).hexdigest(),
                timeline["manifest_file_sha256"],
            )
            self.assertEqual(
                manifest["content_address"]["sha256"],
                timeline["manifest_content_sha256"],
            )
            self.assertIn(
                "not inferred", timeline["server_realm_source"]
            )

    def test_timeline_source_requires_explicit_server_and_realm(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path, _, _ = _write_timeline_manifest(
                Path(temporary) / "offline_data",
                [_warrior_observation("托尼牛", GUID_TONY)],
            )
            for server, realm in ((None, None), ("Capybara", None), (None, "Basin")):
                with self.subTest(server=server, realm=realm):
                    with self.assertRaises(sync_v1.CharacterSyncError):
                        sync_v1.resolve_character_queries(
                            players_from_team_timeline_manifest=manifest_path,
                            default_server=server,
                            default_realm=realm,
                        )

    def test_timeline_identity_ambiguities_fail_closed(self) -> None:
        cases = {
            "same_guid_multiple_names": [
                _warrior_observation("托尼牛", GUID_TONY),
                _warrior_observation("另一个名字", GUID_TONY),
            ],
            "same_name_multiple_guids": [
                _warrior_observation("托尼牛", GUID_TONY),
                _warrior_observation("托尼牛", GUID_TAO),
            ],
            "empty_name": [_warrior_observation(" ", GUID_TONY)],
        }
        for name, observations in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                manifest_path, _, _ = _write_timeline_manifest(
                    Path(temporary) / "offline_data", observations
                )
                with self.assertRaises(sync_v1.CharacterSyncError):
                    sync_v1.character_queries_from_team_timeline_manifest(
                        manifest_path,
                        default_server="Capybara",
                        default_realm="Basin of Stars",
                    )

    def test_timeline_manual_merge_rejects_normalized_path_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path, _, _ = _write_timeline_manifest(
                Path(temporary) / "offline_data",
                [_warrior_observation("Tony", GUID_TONY)],
            )
            with self.assertRaises(sync_v1.CharacterSyncError):
                sync_v1.resolve_character_queries(
                    [
                        history_v1.CharacterQuery(
                            server="capybara",
                            realm="basin of stars",
                            character="TONY",
                        )
                    ],
                    players_from_team_timeline_manifest=manifest_path,
                    default_server="Capybara",
                    default_realm="Basin of Stars",
                )

    def test_full_sync_paginates_uses_guid_deduplicates_shared_raid_and_is_idempotent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "offline_data"
            fixture = ChronicleFixture()
            client = _client(fixture)
            queries = [_query("托尼牛"), _query("桃姬儿")]
            first = sync_v1.sync_character_histories(
                queries,
                data_root=data_root,
                client=client,
                upload_after="2026-09-02T14:26:54Z",
                instance_names=["Upper Tower of Karazhan"],
                required_streams=sync_v1.DEFAULT_REQUIRED_STREAMS,
                page_size=1,
                max_pages=4,
                max_fetch_instances=10,
            )
            self.assertEqual("COMPLETE", first["status"])
            self.assertEqual(3, first["final_inventory_summary"]["player_instance_count"])
            self.assertEqual(2, first["final_inventory_summary"]["unique_instance_count"])
            self.assertEqual(0, first["final_inventory_summary"]["training_fetch_instance_count"])
            self.assertEqual(0, first["final_inventory_summary"]["diagnostic_fetch_instance_count"])
            self.assertEqual([INSTANCE_A, INSTANCE_B], sorted(fixture.metadata_requests))
            self.assertEqual([INSTANCE_A, INSTANCE_B], sorted(fixture.ranking_requests))
            self.assertEqual(
                set(sync_v1.DEFAULT_REQUIRED_STREAMS),
                {
                    stream
                    for instance_id, stream in fixture.event_requests
                    if instance_id == INSTANCE_A
                },
            )
            self.assertFalse(
                any(instance_id == INSTANCE_B for instance_id, _ in fixture.event_requests)
            )
            self.assertEqual(
                len(sync_v1.DEFAULT_REQUIRED_STREAMS), len(fixture.event_requests)
            )
            published, _ = history_v1.load_character_instance_inventory_manifest(
                first["inventory_publication"]["manifest_path"], data_root=data_root
            )
            shared_rows = [
                row for row in published["instances"] if row["instance_id"] == INSTANCE_A
            ]
            self.assertEqual(2, len(shared_rows))
            self.assertTrue(
                all(
                    row["coverage"]["ranking_exact_dps"]["identity_match"]
                    == "EXACT_PLAYER_GUID_ONLY"
                    for row in shared_rows
                )
            )
            self.assertTrue(any("%E6%89%98%E5%B0%BC%E7%89%9B" in url for url in fixture.urls))
            self.assertTrue(any("page=2" in url and "/characters/" in url for url in fixture.urls))
            self.assertFalse(any("leaderboards" in url for url in fixture.urls))
            self.assertTrue(
                all(
                    "upload_after=" in url and "uploaded_after=" not in url
                    for url in fixture.urls
                    if urlparse(url).path.endswith("/raidlogs/recent")
                )
            )

            requests_after_first = (
                len(fixture.metadata_requests),
                len(fixture.ranking_requests),
                len(fixture.event_requests),
            )
            legacy = (
                data_root
                / "chronicle_raw"
                / "external_api"
                / "v1"
                / "manifests"
                / "legacy.json"
            )
            legacy.write_bytes(
                ingest_v1._canonical_json_bytes(
                    {
                        "schema": ingest_v1.SCHEMA,
                        "kind": "chronicle_external_api_raw_snapshot",
                        "instances": [],
                    }
                )
            )
            self.assertNotIn(
                legacy.resolve(), sync_v1.discover_ingest_manifests(data_root)
            )
            second = sync_v1.sync_character_histories(
                queries,
                data_root=data_root,
                client=client,
                instance_names=["Upper Tower of Karazhan"],
                required_streams=sync_v1.DEFAULT_REQUIRED_STREAMS,
                page_size=1,
                max_pages=4,
                max_fetch_instances=10,
            )
            self.assertEqual("COMPLETE", second["status"])
            self.assertEqual([], second["executed_batches"])
            self.assertEqual([], second["remaining_gap_batches"])
            self.assertEqual(
                requests_after_first,
                (
                    len(fixture.metadata_requests),
                    len(fixture.ranking_requests),
                    len(fixture.event_requests),
                ),
                "second sync must skip hash/size-verified ranking and stream objects",
            )
            self.assertEqual(
                [INSTANCE_A],
                second["recent_probe"]["inclusive_boundary_duplicate_ids"],
            )

    def test_history_tier_writes_snapshot_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "offline_data"
            fixture = ChronicleFixture()
            result = sync_v1.sync_character_histories(
                [_query("托尼牛"), _query("桃姬儿")],
                data_root=data_root,
                client=_client(fixture),
                upload_after="2026-09-02T14:26:54Z",
                instance_names=["Upper Tower of Karazhan"],
                evidence_tier="history",
                page_size=1,
                max_pages=4,
            )
            self.assertEqual("COMPLETE", result["status"])
            self.assertEqual("history", result["evidence_tier"])
            self.assertEqual([], fixture.metadata_requests)
            self.assertEqual([], fixture.ranking_requests)
            self.assertEqual([], fixture.event_requests)
            self.assertEqual(0, result["network_contract"]["ranking_requests"])
            self.assertEqual(0, result["network_contract"]["event_stream_requests"])
            self.assertIsNone(result["initial_inventory_summary"])
            self.assertIsNone(result["inventory_publication"])
            self.assertTrue(Path(result["character_capture"]["manifest_path"]).is_file())

    def test_ranking_tier_fetches_exact_rankings_and_never_streams(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "offline_data"
            fixture = ChronicleFixture()
            result = sync_v1.sync_character_histories(
                [_query("托尼牛"), _query("桃姬儿")],
                data_root=data_root,
                client=_client(fixture),
                upload_after="2026-09-02T14:26:54Z",
                instance_names=["Upper Tower of Karazhan"],
                evidence_tier="ranking",
                page_size=1,
                max_pages=4,
                max_fetch_instances=10,
            )
            self.assertEqual("COMPLETE", result["status"])
            self.assertEqual("ranking", result["evidence_tier"])
            self.assertEqual([INSTANCE_A, INSTANCE_B], sorted(fixture.metadata_requests))
            self.assertEqual([INSTANCE_A, INSTANCE_B], sorted(fixture.ranking_requests))
            self.assertEqual([], fixture.event_requests)
            self.assertEqual(2, result["network_contract"]["ranking_requests"])
            self.assertEqual(0, result["network_contract"]["event_stream_requests"])
            self.assertTrue(
                all(
                    not execution["batch"]["stream_types"]
                    for execution in result["executed_batches"]
                )
            )
            fixture.metadata_requests.clear()
            fixture.ranking_requests.clear()
            rerun = sync_v1.sync_character_histories(
                [_query("托尼牛"), _query("桃姬儿")],
                data_root=data_root,
                client=_client(fixture),
                instance_names=["Upper Tower of Karazhan"],
                evidence_tier="ranking",
                page_size=1,
                max_pages=4,
                max_fetch_instances=0,
            )
            self.assertEqual("COMPLETE", rerun["status"])
            self.assertEqual([], rerun["executed_batches"])
            self.assertEqual([], fixture.metadata_requests)
            self.assertEqual([], fixture.ranking_requests)
            self.assertEqual([], fixture.event_requests)
            self.assertGreater(
                rerun["final_inventory_summary"]["training_fetch_instance_count"],
                0,
                "stream-only gaps remain visible but cannot enter the ranking plan",
            )

    def test_wrong_expected_guid_fails_immediately_before_pages_or_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "offline_data"
            fixture = ChronicleFixture()
            fixture.identities["托尼牛"]["guid"] = GUID_TAO
            query = _query("托尼牛")
            with self.assertRaisesRegex(sync_v1.CharacterSyncError, "live GUID mismatch"):
                sync_v1.sync_character_histories(
                    [query],
                    data_root=data_root,
                    client=_client(fixture),
                    upload_after="2026-09-02T14:26:54Z",
                    expected_guids={query: GUID_TONY},
                    evidence_tier="history",
                    page_size=1,
                    max_pages=4,
                )
            self.assertEqual(1, len(fixture.urls))
            self.assertIn("/characters/", fixture.urls[0])
            self.assertNotIn("/instances", fixture.urls[0])
            self.assertEqual([], fixture.metadata_requests)
            self.assertEqual([], fixture.ranking_requests)
            self.assertEqual([], fixture.event_requests)
            self.assertFalse(data_root.exists())

    def test_cli_defaults_to_eligible_streams(self) -> None:
        args = sync_v1._parser().parse_args(
            ["audit", "--player", "Capybara", "Basin of Stars", "托尼牛"]
        )
        self.assertEqual("eligible-streams", args.evidence_tier)

    def test_bootstrap_without_snapshot_requires_explicit_upload_after(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(sync_v1.CharacterSyncSafetyLimitError):
                sync_v1.audit_character_sync(
                    [_query("托尼牛")],
                    data_root=Path(temporary) / "offline_data",
                    client=_client(lambda *args: self.fail("network must not run")),
                )


if __name__ == "__main__":
    unittest.main()
