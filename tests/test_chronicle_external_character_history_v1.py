from __future__ import annotations

from copy import deepcopy
import gzip
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import unquote, urlparse

from o2o_dps import chronicle_external_api_ingest_v1 as ingest_v1
from o2o_dps import chronicle_external_character_history_v1 as history_v1
from o2o_dps.chronicle_external_api_ingest_v1 import (
    ChronicleClient,
    HTTPResponse,
    RawObjectStore,
)
from o2o_dps.chronicle_external_character_history_v1 import (
    CharacterHistoryError,
    CharacterHistorySafetyLimitError,
    CharacterQuery,
    build_character_instance_inventory,
    capture_character_histories,
    list_character_histories,
    load_character_instance_inventory_manifest,
    load_character_history_manifest,
    publish_character_instance_inventory,
)


GUID_TONY = "0x000000000056DEB3"
GUID_TAO = "0x00000000007395BF"
INSTANCE_A = "11111111-1111-1111-1111-111111111111"
INSTANCE_B = "22222222-2222-2222-2222-222222222222"
INSTANCE_C = "33333333-3333-3333-3333-333333333333"
INSTANCE_D = "44444444-4444-4444-4444-444444444444"


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _response(url: str, value: object) -> HTTPResponse:
    return HTTPResponse(
        status=200,
        headers={"content-type": "application/json"},
        body=_json_bytes(value),
        url=url,
    )


class Router:
    def __init__(self, handler):
        self.handler = handler
        self.urls: list[str] = []

    def __call__(self, method, url, headers, timeout):
        self.urls.append(url)
        return self.handler(method, url, headers, timeout)


def _client(handler) -> tuple[ChronicleClient, Router]:
    router = Router(handler)
    return (
        ChronicleClient(
            transport=router,
            max_attempts=1,
            request_interval_seconds=0,
        ),
        router,
    )


def _identity(
    name: str = "托 尼牛", guid: str = GUID_TONY
) -> dict[str, object]:
    return {
        "guid": guid,
        "name": name,
        "class": "Warrior",
        "race": "Tauren",
        "gender": "Male",
        "level": 60,
        "spec": "Arms",
        "role": "dps",
        "item_level": 78.5,
        "server": {"id": "server-capy", "name": "Capybara"},
        "realm": {
            "id": "realm-basin",
            "server_id": "server-capy",
            "name": "Basin of Stars",
        },
        "guild": {"id": "guild-nanbei", "name": "南北"},
        "updated_at": "2026-09-11T01:00:00Z",
    }


def _log(
    instance_id: str,
    *,
    slug: str,
    started_at: str,
    uploaded_at: str,
    guild: str | None = "南北",
    name: str = "Upper Tower of Karazhan",
) -> dict[str, object]:
    return {
        "id": instance_id,
        "slug": slug,
        "name": name,
        "guild": None if guild is None else {"id": f"guild-{slug}", "name": guild},
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


def _page(
    identity: dict[str, object],
    logs: list[dict[str, object]],
    *,
    page: int,
    has_more: bool,
    page_size: int = 50,
) -> dict[str, object]:
    return {
        "character": deepcopy(identity),
        "logs": deepcopy(logs),
        "pagination": {
            "page": page,
            "page_size": page_size,
            "has_more": has_more,
        },
    }


def _history_handler(
    identities: dict[str, dict[str, object]],
    pages: dict[str, list[list[dict[str, object]]]],
):
    def handler(method, url, headers, timeout):
        parsed = urlparse(url)
        parts = [unquote(value) for value in parsed.path.split("/")]
        character = parts[-2] if parts[-1] == "instances" else parts[-1]
        identity = identities[character]
        if parts[-1] != "instances":
            return _response(url, identity)
        query = dict(
            pair.split("=", 1) for pair in parsed.query.split("&") if "=" in pair
        )
        page_number = int(query["page"])
        character_pages = pages[character]
        return _response(
            url,
            _page(
                identity,
                character_pages[page_number - 1],
                page=page_number,
                has_more=page_number < len(character_pages),
                page_size=int(query["page_size"]),
            ),
        )

    return handler


def _query(character: str = "托 尼牛") -> CharacterQuery:
    return CharacterQuery(
        server="Capybara", realm="Basin of Stars", character=character
    )


def _capture_fixture(
    data_root: Path,
    *,
    logs: list[dict[str, object]] | None = None,
    identity: dict[str, object] | None = None,
) -> dict[str, object]:
    identity = identity or _identity()
    logs = logs or [
        _log(
            INSTANCE_A,
            slug="slug-a",
            started_at="2026-09-03T12:48:06.919Z",
            uploaded_at="2026-09-03T14:39:03.000507Z",
        )
    ]
    client, _ = _client(
        _history_handler({str(identity["name"]): identity}, {str(identity["name"]): [logs]})
    )
    return capture_character_histories(
        [_query(str(identity["name"]))], data_root=data_root, client=client
    )


def _write_ingest_manifest(
    data_root: Path,
    rows: list[dict[str, object]],
    *,
    leaderboard_entries: list[dict[str, object]] | None = None,
) -> Path:
    store = RawObjectStore(data_root)
    instances: list[dict[str, object]] = []
    for row in rows:
        instance_id = str(row["instance_id"])
        metadata = {
            "id": instance_id,
            "slug": row["slug"],
            "name": "Upper Tower of Karazhan",
            "guild": {"id": "guild-nanbei", "name": "南北"},
        }
        metadata_body = _json_bytes(metadata)
        ranking_rows = row.get("ranking_rows")
        if ranking_rows is None:
            ranking_rows = [
                {
                    "id": f"ranking-{instance_id}",
                    "encounter_id": "encounter-1",
                    "encounter_name": "Mephistroth",
                    "player_guid": GUID_TONY,
                    "player_name": "display name is not identity",
                    "player_spec": "Arms",
                    "player_role": "dps",
                    "killed_at": "2026-09-02T02:00:00Z",
                    "damage_done": 120000,
                    "duration_secs": 100.0,
                    "dps": 1200.0,
                }
            ]
        if not isinstance(ranking_rows, list):
            raise AssertionError("ranking_rows fixture must be a list")
        ranking_body = _json_bytes(ranking_rows)
        streams: dict[str, object] = {}
        for stream in row.get("streams", []):
            payload = gzip.compress(
                (
                    f"{instance_id}:{stream}:"
                    f"{row.get('stream_payload_tag', 'default')}"
                ).encode(),
                mtime=0,
            )
            streams[str(stream)] = {
                "status": "AVAILABLE",
                "object": store.put(
                    payload,
                    suffix=f"{stream}.events.gz",
                    media_type="application/octet-stream",
                ),
            }
        for stream in row.get("unavailable_streams", []):
            if str(stream) in streams:
                raise AssertionError(
                    "stream fixture cannot be AVAILABLE and UNAVAILABLE_404 "
                    "inside one manifest row"
                )
            streams[str(stream)] = {
                "status": "UNAVAILABLE_404",
                "object": None,
                "note": "official API permits unavailable streams for older instances",
            }
        ranking = None
        if row.get("ranking", True):
            ranking = {
                "object": store.put(
                    ranking_body, suffix="json", media_type="application/json"
                ),
                "record_count": len(ranking_rows),
            }
        metadata_ref = None
        if row.get("metadata", True):
            metadata_ref = {
                "request_url": f"https://example/{instance_id}",
                "object": store.put(
                    metadata_body, suffix="json", media_type="application/json"
                ),
            }
        instances.append(
            {
                "instance_id": instance_id,
                "slug": row["slug"],
                "instance_name": "Upper Tower of Karazhan",
                "started_at": row.get("started_at", "2026-09-02T01:00:00Z"),
                "instance_contamination_label": row.get(
                    "contamination", "POSTFIX_KNOWN_CLEAN"
                ),
                "metadata": metadata_ref,
                "ranking_records": ranking,
                "streams": streams,
            }
        )
    manifest = {
        "schema": ingest_v1.SCHEMA,
        "implementation_revision": ingest_v1.IMPLEMENTATION_REVISION,
        "parser_contract_revision": ingest_v1.PARSER_CONTRACT_REVISION,
        "kind": "chronicle_external_api_raw_snapshot",
        "api_base": ingest_v1.EXTERNAL_API_BASE,
        "request": {},
        "recent_pages": [],
        "leaderboard_snapshots": leaderboard_entries or [],
        "instances": instances,
    }
    payload = ingest_v1._canonical_json_bytes(manifest)
    path = store.root / "manifests" / f"{hashlib.sha256(payload).hexdigest()}.json"
    ingest_v1._atomic_write_bytes(path, payload)
    return path


class ChronicleExternalCharacterHistoryV1Tests(unittest.TestCase):
    def test_capture_paginates_encodes_deduplicates_and_is_order_invariant(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "offline_data"
            tony = _identity("托 尼牛", GUID_TONY)
            tao = _identity("桃姬儿", GUID_TAO)
            newest = _log(
                INSTANCE_A,
                slug="slug-a",
                started_at="2026-09-09T00:00:00Z",
                uploaded_at="2026-09-10T00:00:00Z",
            )
            older = _log(
                INSTANCE_B,
                slug="slug-b",
                started_at="2026-09-01T00:00:00Z",
                uploaded_at="2026-09-02T00:00:00Z",
            )
            handler = _history_handler(
                {"托 尼牛": tony, "桃姬儿": tao},
                {
                    "托 尼牛": [[newest], [deepcopy(newest), older]],
                    "桃姬儿": [[deepcopy(newest)]],
                },
            )
            client, router = _client(handler)
            first = capture_character_histories(
                [_query("桃姬儿"), _query("托 尼牛")],
                data_root=data_root,
                client=client,
                max_pages=3,
            )
            second_client, _ = _client(handler)
            second = capture_character_histories(
                [_query("托 尼牛"), _query("桃姬儿")],
                data_root=data_root,
                client=second_client,
                max_pages=3,
            )
            self.assertEqual(first["content_sha256"], second["content_sha256"])
            self.assertEqual(first["manifest_path"], second["manifest_path"])
            manifest, resolved = load_character_history_manifest(
                first["manifest_path"], data_root=data_root
            )
            self.assertEqual(resolved, Path(first["manifest_path"]))
            tony_row = next(
                row
                for row in manifest["characters"]
                if row["query"]["character"] == "托 尼牛"
            )
            self.assertEqual(
                [INSTANCE_A, INSTANCE_B],
                [row["instance_id"] for row in tony_row["instances"]],
            )
            self.assertEqual(2, tony_row["page_count"])
            self.assertTrue(any("Basin%20of%20Stars" in url for url in router.urls))
            self.assertTrue(any("%E6%89%98%20%E5%B0%BC%E7%89%9B" in url for url in router.urls))

    def test_list_is_read_only_and_max_pages_and_duplicate_conflicts_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "offline_data"
            identity = _identity()
            log = _log(
                INSTANCE_A,
                slug="slug-a",
                started_at="2026-09-02T00:00:00Z",
                uploaded_at="2026-09-03T00:00:00Z",
            )
            handler = _history_handler(
                {"托 尼牛": identity}, {"托 尼牛": [[log]]}
            )
            client, _ = _client(handler)
            listed = list_character_histories([_query()], client=client)
            self.assertEqual(1, listed["summary"]["unique_instance_count"])
            self.assertFalse(data_root.exists())

            endless_handler = _history_handler(
                {"托 尼牛": identity}, {"托 尼牛": [[log], [log]]}
            )
            endless_client, _ = _client(endless_handler)
            with self.assertRaises(CharacterHistorySafetyLimitError):
                capture_character_histories(
                    [_query()],
                    data_root=data_root,
                    client=endless_client,
                    max_pages=1,
                )
            self.assertFalse(
                (data_root / "chronicle_raw" / "external_api" / "v1" / "character_history_manifests").exists()
            )

            changed = deepcopy(log)
            changed["uploaded_at"] = "2026-09-04T00:00:00Z"
            conflict_handler = _history_handler(
                {"托 尼牛": identity},
                {"托 尼牛": [[log], [changed]]},
            )
            conflict_client, _ = _client(conflict_handler)
            with self.assertRaisesRegex(CharacterHistoryError, "duplicate instance"):
                capture_character_histories(
                    [_query()],
                    data_root=data_root,
                    client=conflict_client,
                    max_pages=2,
                )

    def test_page_character_guid_conflict_fails(self) -> None:
        identity = _identity()

        def handler(method, url, headers, timeout):
            if urlparse(url).path.endswith("/instances"):
                wrong = _identity(guid=GUID_TAO)
                return _response(url, _page(wrong, [], page=1, has_more=False))
            return _response(url, identity)

        client, _ = _client(handler)
        with self.assertRaisesRegex(CharacterHistoryError, "identity|GUID"):
            list_character_histories([_query()], client=client)

    def test_malformed_identity_and_zero_character_instance_id_fail_closed(self) -> None:
        client, _ = _client(lambda method, url, headers, timeout: _response(url, []))
        with self.assertRaisesRegex(CharacterHistoryError, "JSON object"):
            list_character_histories([_query()], client=client)

        identity = _identity()
        zero = _log(
            "00000000-0000-0000-0000-000000000000",
            slug="zero-slug",
            started_at="2026-09-02T00:00:00Z",
            uploaded_at="2026-09-03T00:00:00Z",
        )
        zero_client, _ = _client(
            _history_handler({"托 尼牛": identity}, {"托 尼牛": [[zero]]})
        )
        with self.assertRaisesRegex(CharacterHistoryError, "zero instance id"):
            list_character_histories([_query()], client=zero_client)

    def test_absent_performance_is_retained_as_missing_parse_evidence(self) -> None:
        identity = _identity(name="Ci", guid="0x000000000008112D")
        normal = _log(
            INSTANCE_A,
            slug="slug-a",
            started_at="2026-09-02T00:00:00Z",
            uploaded_at="2026-09-03T00:00:00Z",
        )
        absent = _log(
            INSTANCE_B,
            slug="slug-b",
            started_at="2026-08-09T12:34:15.89Z",
            uploaded_at="2026-08-09T13:53:31.590591Z",
            name="Naxxramas",
            guild="养老生活",
        )
        absent.pop("performance")
        client, _ = _client(
            _history_handler({"Ci": identity}, {"Ci": [[normal, absent]]})
        )
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "offline_data"
            capture = capture_character_histories(
                [_query("Ci")], data_root=data_root, client=client
            )
            manifest, _ = load_character_history_manifest(
                capture["manifest_path"], data_root=data_root
            )
            instances = manifest["characters"][0]["instances"]
            self.assertEqual(2, len(instances), "the absent field must not drop its log")
            row = next(item for item in instances if item["instance_id"] == INSTANCE_B)
            self.assertEqual([], row["performance"])
            self.assertEqual(
                {
                    "field": "performance",
                    "log_id": INSTANCE_B,
                },
                row["performance_provenance"]["raw_page_lookup"],
            )
            self.assertEqual(
                "ABSENT", row["performance_provenance"]["raw_field_state"]
            )
            self.assertEqual(
                "MISSING_PARSE_EVIDENCE_NOT_EMPTY_PARSE_RESULT",
                row["performance_provenance"]["interpretation"],
            )
            raw_root = RawObjectStore(data_root).root
            page_ref = manifest["characters"][0]["pages"][0]["object"]
            page_bytes = ingest_v1._read_object_reference(
                raw_root, page_ref, label="Ci page 1"
            )
            raw_page = json.loads(page_bytes.decode("utf-8"))
            raw_absent = next(
                item for item in raw_page["logs"] if item["id"] == INSTANCE_B
            )
            self.assertNotIn("performance", raw_absent)

        explicit_null = deepcopy(absent)
        explicit_null["performance"] = None
        null_client, _ = _client(
            _history_handler({"Ci": identity}, {"Ci": [[explicit_null]]})
        )
        with self.assertRaisesRegex(CharacterHistoryError, "performance must be a JSON list"):
            list_character_histories([_query("Ci")], client=null_client)

    def test_character_guid_requires_exact_nonzero_0x16_format(self) -> None:
        for guid in (
            "0x0000000000000000",
            "000000000056DEB3",
            "0x56DEB3",
            "0x000000000056DEBG",
        ):
            with self.subTest(guid=guid):
                identity = _identity(guid=guid)
                client, _ = _client(
                    _history_handler({"托 尼牛": identity}, {"托 尼牛": [[]]})
                )
                with self.assertRaisesRegex(
                    CharacterHistoryError, "0x plus 16|all-zero"
                ):
                    list_character_histories([_query()], client=client)

    def test_redirected_identity_or_page_response_fails_before_publication(self) -> None:
        identity = _identity(name="托尼牛")
        log = _log(
            INSTANCE_A,
            slug="slug-a",
            started_at="2026-09-02T00:00:00Z",
            uploaded_at="2026-09-03T00:00:00Z",
        )

        def redirected_identity(method, url, headers, timeout):
            return _response("https://redirect.invalid/identity", identity)

        def redirected_page(method, url, headers, timeout):
            if urlparse(url).path.endswith("/instances"):
                return _response(
                    "https://redirect.invalid/page",
                    _page(identity, [log], page=1, has_more=False),
                )
            return _response(url, identity)

        with tempfile.TemporaryDirectory() as temporary:
            for handler in (redirected_identity, redirected_page):
                with self.subTest(handler=handler.__name__):
                    data_root = Path(temporary) / handler.__name__ / "offline_data"
                    client, _ = _client(handler)
                    with self.assertRaisesRegex(CharacterHistoryError, "response URL"):
                        capture_character_histories(
                            [_query("托尼牛")], data_root=data_root, client=client
                        )
                    self.assertFalse(data_root.exists())

    def test_multi_spec_trash_rankings_use_record_id_and_preserve_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "offline_data"
            tao = _identity(name="桃姬儿", guid=GUID_TAO)
            log = _log(
                INSTANCE_A,
                slug="slug-a",
                started_at="2026-09-02T00:00:00Z",
                uploaded_at="2026-09-03T00:00:00Z",
            )
            capture = _capture_fixture(data_root, logs=[log], identity=tao)
            rankings = [
                {
                    "id": "ranking-trash-fury",
                    "encounter_id": None,
                    "encounter_name": "Trash",
                    "player_guid": GUID_TAO,
                    "player_name": "桃姬儿",
                    "player_spec": "Fury",
                    "player_role": "dps",
                    "killed_at": None,
                    "damage_done": 49632,
                    "duration_secs": 81.856,
                    "dps": 606.3330727130572,
                },
                {
                    "id": "ranking-trash-arms",
                    "encounter_id": None,
                    "encounter_name": "Trash",
                    "player_guid": GUID_TAO,
                    "player_name": "桃姬儿",
                    "player_spec": "Arms",
                    "player_role": "dps",
                    "killed_at": "2026-09-02T02:00:00Z",
                    "damage_done": 1492074,
                    "duration_secs": 1143.68,
                    "dps": 1304.6254196978173,
                },
            ]
            ingest = _write_ingest_manifest(
                data_root,
                [
                    {
                        "instance_id": INSTANCE_A,
                        "slug": "slug-a",
                        "streams": [],
                        "ranking_rows": rankings,
                    }
                ],
            )
            inventory = build_character_instance_inventory(
                [capture["manifest_path"]],
                [ingest],
                data_root=data_root,
                required_streams=("damage",),
            )
            records = inventory["instances"][0]["coverage"]["ranking_exact_dps"][
                "records"
            ]
            self.assertEqual(2, len(records))
            self.assertEqual(
                {"ranking-trash-arms", "ranking-trash-fury"},
                {record["ranking_record_id"] for record in records},
            )
            self.assertEqual({"Arms", "Fury"}, {record["player_spec"] for record in records})
            self.assertTrue(all(record["player_role"] == "dps" for record in records))
            self.assertEqual(
                {None, "2026-09-02T02:00:00Z"},
                {record["killed_at"] for record in records},
            )
            conflicting_id = deepcopy(rankings)
            conflicting_id[1]["id"] = conflicting_id[0]["id"]
            with self.assertRaisesRegex(CharacterHistoryError, "ranking record id"):
                history_v1._exact_ranking_records(
                    conflicting_id, character_guid=GUID_TAO
                )

    def test_ranking_endpoint_negative_evidence_is_replayable_and_not_refetched(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "offline_data"
            logs = [
                _log(
                    INSTANCE_A,
                    slug="captured-absent",
                    started_at="2026-09-03T12:48:06.919Z",
                    uploaded_at="2026-09-03T14:39:03.000507Z",
                ),
                _log(
                    INSTANCE_B,
                    slug="endpoint-not-captured",
                    started_at="2026-09-04T01:00:00Z",
                    uploaded_at="2026-09-04T03:00:00Z",
                ),
                _log(
                    INSTANCE_C,
                    slug="exact-present",
                    started_at="2026-09-04T01:00:00Z",
                    uploaded_at="2026-09-05T01:00:00Z",
                ),
            ]
            capture = _capture_fixture(data_root, logs=logs)
            wrong_player_ranking = [
                {
                    "id": "ranking-other-player",
                    "encounter_id": "encounter-1",
                    "encounter_name": "Mephistroth",
                    "player_guid": GUID_TAO,
                    "player_name": "桃姬儿",
                    "player_spec": "Fury",
                    "player_role": "dps",
                    "killed_at": "2026-09-02T02:00:00Z",
                    "damage_done": 99999,
                    "duration_secs": 100.0,
                    "dps": 999.99,
                }
            ]
            first_ingest = _write_ingest_manifest(
                data_root,
                [
                    {
                        "instance_id": INSTANCE_A,
                        "slug": "captured-absent",
                        "streams": ["damage"],
                        "ranking_rows": wrong_player_ranking,
                    },
                    {
                        "instance_id": INSTANCE_B,
                        "slug": "endpoint-not-captured",
                        "streams": ["damage"],
                        "ranking": False,
                    },
                    {
                        "instance_id": INSTANCE_C,
                        "slug": "exact-present",
                        "streams": ["damage"],
                    },
                ],
            )
            repeated_ingest = _write_ingest_manifest(
                data_root,
                [
                    {
                        "instance_id": INSTANCE_A,
                        "slug": "captured-absent",
                        "streams": ["damage", "spell_go"],
                        "ranking_rows": wrong_player_ranking,
                    }
                ],
            )
            ingest_paths = [first_ingest, repeated_ingest]
            inventory = build_character_instance_inventory(
                [capture["manifest_path"]],
                ingest_paths,
                data_root=data_root,
                required_streams=("damage",),
                max_fetch_instances=10,
            )
            by_id = {row["instance_id"]: row for row in inventory["instances"]}

            captured_absent = by_id[INSTANCE_A]
            ranking = captured_absent["coverage"]["ranking_exact_dps"]
            self.assertEqual(
                "RANKING_ENDPOINT_CAPTURED_EXACT_GUID_ABSENT",
                ranking["status"],
            )
            self.assertTrue(ranking["ranking_endpoint_captured"])
            self.assertEqual(2, ranking["ranking_endpoint_capture_count"])
            self.assertFalse(ranking["exact_dps_available"])
            self.assertFalse(ranking["usable_for_exact_dps_training"])
            self.assertFalse(ranking["ranking_fetch_required"])
            self.assertEqual([], ranking["records"])
            self.assertTrue(
                captured_absent["missing_plan"]["ranking_exact_dps"],
                "semantic exact-DPS absence must remain visible",
            )
            self.assertFalse(
                captured_absent["missing_plan"]["ranking_fetch_required"]
            )

            no_object = by_id[INSTANCE_B]
            self.assertEqual(
                "MISSING", no_object["coverage"]["ranking_exact_dps"]["status"]
            )
            self.assertTrue(no_object["missing_plan"]["ranking_exact_dps"])
            self.assertTrue(no_object["missing_plan"]["ranking_fetch_required"])

            exact = by_id[INSTANCE_C]
            self.assertEqual(
                "EXACT_RANKING_DPS_AVAILABLE",
                exact["coverage"]["ranking_exact_dps"]["status"],
            )
            self.assertTrue(
                exact["coverage"]["ranking_exact_dps"]["exact_dps_available"]
            )
            self.assertFalse(exact["missing_plan"]["ranking_exact_dps"])
            self.assertFalse(exact["missing_plan"]["ranking_fetch_required"])
            self.assertEqual(
                [INSTANCE_B],
                inventory["fetch_plan"]["training"]["explicit_instance_ids"],
            )

            from o2o_dps import chronicle_external_character_sync_v1 as sync_v1

            batches = sync_v1.build_gap_batches(
                inventory, evidence_tier="ranking"
            )
            self.assertEqual(1, len(batches))
            self.assertEqual((INSTANCE_B,), batches[0].instance_ids)

            with self.assertRaisesRegex(
                CharacterHistoryError, "replay legacy raw history first"
            ):
                build_character_instance_inventory(
                    [capture["manifest_path"]],
                    ingest_paths,
                    data_root=data_root,
                    required_streams=("damage",),
                    max_fetch_instances=10,
                    _implementation_revision=(
                        history_v1.RANKING_NEGATIVE_EVIDENCE_INVENTORY_IMPLEMENTATION_REVISION
                    ),
                )

            first_publication = publish_character_instance_inventory(
                [capture["manifest_path"]],
                ingest_paths,
                data_root=data_root,
                required_streams=("damage",),
                max_fetch_instances=10,
            )
            second_publication = publish_character_instance_inventory(
                [capture["manifest_path"]],
                list(reversed(ingest_paths)),
                data_root=data_root,
                required_streams=("damage",),
                max_fetch_instances=10,
            )
            self.assertEqual(first_publication, second_publication)
            replayed, _ = load_character_instance_inventory_manifest(
                first_publication["manifest_path"], data_root=data_root
            )
            self.assertEqual(inventory, replayed)

    def test_inventory_404_stream_is_terminal_and_available_upgrade_wins(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "offline_data"
            logs = [
                _log(
                    INSTANCE_A,
                    slug="terminal-404",
                    started_at="2026-09-02T01:00:00Z",
                    uploaded_at="2026-09-03T01:00:00Z",
                ),
                _log(
                    INSTANCE_B,
                    slug="never-captured",
                    started_at="2026-09-04T01:00:00Z",
                    uploaded_at="2026-09-05T01:00:00Z",
                ),
                _log(
                    INSTANCE_C,
                    slug="later-available",
                    started_at="2026-09-06T01:00:00Z",
                    uploaded_at="2026-09-07T01:00:00Z",
                ),
            ]
            capture = _capture_fixture(data_root, logs=logs)
            first_ingest = _write_ingest_manifest(
                data_root,
                [
                    {
                        "instance_id": INSTANCE_A,
                        "slug": "terminal-404",
                        "streams": ["damage"],
                        "unavailable_streams": ["spell_go"],
                    },
                    {
                        "instance_id": INSTANCE_B,
                        "slug": "never-captured",
                        "streams": ["damage"],
                    },
                    {
                        "instance_id": INSTANCE_C,
                        "slug": "later-available",
                        "streams": ["damage"],
                        "unavailable_streams": ["spell_go"],
                    },
                ],
            )
            later_ingest = _write_ingest_manifest(
                data_root,
                [
                    {
                        "instance_id": INSTANCE_C,
                        "slug": "later-available",
                        "streams": ["spell_go"],
                    }
                ],
            )
            inventory = build_character_instance_inventory(
                [capture["manifest_path"]],
                [first_ingest, later_ingest],
                data_root=data_root,
                required_streams=("damage", "spell_go"),
                max_fetch_instances=10,
            )
            by_id = {row["instance_id"]: row for row in inventory["instances"]}

            terminal = by_id[INSTANCE_A]
            terminal_coverage = terminal["coverage"]["required_streams"]
            self.assertEqual(["spell_go"], terminal_coverage["missing"])
            self.assertEqual(
                ["spell_go"], terminal_coverage["terminal_unavailable_404"]
            )
            self.assertEqual([], terminal_coverage["fetch_required"])
            self.assertEqual(
                "CAPTURED_NO_FETCH_REQUIRED",
                terminal_coverage["network_capture_status"],
            )
            self.assertEqual(
                ["spell_go"],
                terminal["missing_plan"][
                    "required_streams_terminal_unavailable_404"
                ],
            )
            self.assertEqual(
                [], terminal["missing_plan"]["required_streams_fetch_required"]
            )
            self.assertFalse(terminal["missing_plan"]["training_fetch_planned"])

            uncaptured = by_id[INSTANCE_B]
            uncaptured_coverage = uncaptured["coverage"]["required_streams"]
            self.assertEqual(["spell_go"], uncaptured_coverage["uncaptured"])
            self.assertEqual(["spell_go"], uncaptured_coverage["fetch_required"])
            self.assertEqual([], uncaptured_coverage["terminal_unavailable_404"])
            self.assertTrue(uncaptured["missing_plan"]["training_fetch_planned"])

            upgraded = by_id[INSTANCE_C]["coverage"]["required_streams"]
            self.assertEqual("COMPLETE", upgraded["status"])
            self.assertEqual([], upgraded["missing"])
            self.assertEqual([], upgraded["terminal_unavailable_404"])
            self.assertEqual([], upgraded["fetch_required"])
            self.assertEqual(
                ["spell_go"], upgraded["available_after_prior_unavailable_404"]
            )
            self.assertEqual(
                [INSTANCE_B],
                inventory["fetch_plan"]["training"]["explicit_instance_ids"],
            )
            self.assertEqual(
                ["spell_go"], inventory["fetch_plan"]["training"]["stream_types"]
            )

            from o2o_dps import chronicle_external_character_sync_v1 as sync_v1

            batches = sync_v1.build_gap_batches(
                inventory, evidence_tier="eligible-streams"
            )
            self.assertEqual(1, len(batches))
            self.assertEqual((INSTANCE_B,), batches[0].instance_ids)
            self.assertEqual(("spell_go",), batches[0].stream_types)

    def test_inventory_conflicting_available_stream_objects_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "offline_data"
            capture = _capture_fixture(data_root)
            first_ingest = _write_ingest_manifest(
                data_root,
                [
                    {
                        "instance_id": INSTANCE_A,
                        "slug": "slug-a",
                        "streams": ["damage"],
                        "stream_payload_tag": "first",
                    }
                ],
            )
            conflicting_ingest = _write_ingest_manifest(
                data_root,
                [
                    {
                        "instance_id": INSTANCE_A,
                        "slug": "slug-a",
                        "streams": ["damage"],
                        "stream_payload_tag": "conflicting",
                    }
                ],
            )
            with self.assertRaisesRegex(
                CharacterHistoryError,
                "conflicting AVAILABLE objects for stream damage",
            ):
                build_character_instance_inventory(
                    [capture["manifest_path"]],
                    [first_ingest, conflicting_ingest],
                    data_root=data_root,
                    required_streams=("damage",),
                )

    def test_inventory_separates_parse_exact_ranking_actions_and_fetch_lanes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "offline_data"
            logs = [
                _log(
                    INSTANCE_A,
                    slug="slug-a",
                    started_at="2026-09-03T12:48:06.919Z",
                    uploaded_at="2026-09-03T14:39:03.000507Z",
                ),
                _log(
                    INSTANCE_B,
                    slug="slug-b",
                    started_at="2026-08-30T01:00:00Z",
                    uploaded_at="2026-08-31T01:00:00Z",
                ),
                _log(
                    INSTANCE_C,
                    slug="slug-c",
                    guild="Other Guild",
                    started_at="2026-09-04T01:00:00Z",
                    uploaded_at="2026-09-05T01:00:00Z",
                ),
                _log(
                    INSTANCE_D,
                    slug="slug-d",
                    started_at="2026-09-06T01:00:00Z",
                    uploaded_at="2026-09-07T01:00:00Z",
                ),
            ]
            capture = _capture_fixture(data_root, logs=logs)
            required = ("damage", "spell_start", "spell_go", "spell_fail")
            ingest = _write_ingest_manifest(
                data_root,
                [
                    {
                        "instance_id": INSTANCE_A,
                        "slug": "slug-a",
                        "streams": ["damage"],
                    },
                    {
                        "instance_id": INSTANCE_D,
                        "slug": "slug-d",
                        "streams": list(required),
                    },
                ],
                leaderboard_entries=[
                    {
                        "query": {},
                        "entry_count": 1,
                        "entries": [{"id": "00000000-0000-0000-0000-000000000000"}],
                    }
                ],
            )
            inventory = build_character_instance_inventory(
                [capture["manifest_path"]],
                [ingest],
                data_root=data_root,
                required_streams=required,
                max_fetch_instances=10,
            )
            by_id = {row["instance_id"]: row for row in inventory["instances"]}
            self.assertEqual(
                "PARSE_AVAILABLE_NOT_EXACT_DPS",
                by_id[INSTANCE_C]["coverage"]["performance_parse"]["status"],
            )
            self.assertEqual(
                "EXACT_RANKING_DPS_AVAILABLE",
                by_id[INSTANCE_A]["coverage"]["ranking_exact_dps"]["status"],
            )
            self.assertEqual(
                "MISSING",
                by_id[INSTANCE_C]["coverage"]["ranking_exact_dps"]["status"],
            )
            self.assertEqual(
                ["spell_fail", "spell_go", "spell_start"],
                by_id[INSTANCE_A]["coverage"]["required_streams"]["missing"],
            )
            self.assertEqual(
                "COMPLETE",
                by_id[INSTANCE_D]["coverage"]["action_events"]["status"],
            )
            self.assertEqual(
                [INSTANCE_A, INSTANCE_C],
                inventory["fetch_plan"]["training"]["explicit_instance_ids"],
            )
            self.assertEqual(
                [INSTANCE_B],
                inventory["fetch_plan"]["diagnostic_metadata_and_ranking"][
                    "explicit_instance_ids"
                ],
            )
            self.assertEqual(
                [INSTANCE_B, INSTANCE_C], inventory["missing_instance_ids"]
            )
            self.assertFalse(
                inventory["evidence_contract"]["leaderboard_instance_id_used"]
            )
            self.assertFalse(
                inventory["contamination_contract"]["player_name_used"]
            )

            filtered = build_character_instance_inventory(
                [capture["manifest_path"]],
                [ingest],
                data_root=data_root,
                instance_name="Upper Tower of Karazhan",
                started_at_not_before="2026-09-01T00:00:00Z",
                required_streams=required,
                max_fetch_instances=10,
            )
            self.assertNotIn(
                INSTANCE_B, [row["instance_id"] for row in filtered["instances"]]
            )

    def test_cutoff_and_player_name_invariance(self) -> None:
        before = {
            "guild": {"name": "南北"},
            "started_at": "2026-09-02T15:59:59Z",
            "character_name": "托尼牛",
        }
        boundary = {
            "guild": {"name": "南北"},
            "started_at": "2026-09-02T16:00:00Z",
            "character_name": "桃姬儿",
        }
        after = {
            "guild": {"name": "南北"},
            "started_at": "2026-09-03T04:00:00Z",
            "character_name": "围观群众三爷",
        }
        self.assertEqual(
            "SUSPECT_36YD_RANGE_BUG", history_v1._contamination_for_log(before)
        )
        self.assertEqual(
            "RANGE_BUG_BOUNDARY_UNCERTAIN_NONVOTING",
            history_v1._contamination_for_log(boundary),
        )
        self.assertEqual(
            "POSTFIX_KNOWN_CLEAN", history_v1._contamination_for_log(after)
        )
        renamed = deepcopy(after)
        renamed["character_name"] = "任何名字"
        self.assertEqual(
            history_v1._contamination_for_log(after),
            history_v1._contamination_for_log(renamed),
        )

    def test_legacy_history_audits_and_replays_locally_to_current_boundary(self) -> None:
        from o2o_dps import chronicle_external_character_sync_v1 as sync_v1

        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "offline_data"
            capture = _capture_fixture(
                data_root,
                logs=[
                    _log(
                        INSTANCE_A,
                        slug="legacy-cutoff",
                        started_at="2026-09-02T01:00:00Z",
                        uploaded_at="2026-09-03T01:00:00Z",
                    )
                ],
            )
            current, current_path = load_character_history_manifest(
                capture["manifest_path"], data_root=data_root
            )
            legacy_core = {
                key: deepcopy(value)
                for key, value in current.items()
                if key != "content_address"
            }
            legacy_core["implementation_revision"] = (
                history_v1.LEGACY_IMPLEMENTATION_REVISION
            )
            legacy_core["contamination_contract"] = (
                history_v1._contamination_contract(
                    history_v1.LEGACY_IMPLEMENTATION_REVISION
                )
            )
            for character in legacy_core["characters"]:
                for membership in character["instances"]:
                    membership["contamination_label"] = (
                        ingest_v1.classify_range_bug_legacy_v1(
                            membership["guild"]["name"],
                            membership["started_at"],
                        )
                    )
            legacy = history_v1._content_addressed(legacy_core)
            legacy_sha = history_v1._verify_content_address(
                legacy, label="legacy history fixture"
            )
            legacy_path = current_path.parent / (
                "chronicle_external_character_history_v1."
                f"{legacy_sha}.manifest.json"
            )
            ingest_v1._atomic_write_bytes(
                legacy_path, history_v1._canonical_document_bytes(legacy)
            )
            loaded_legacy, _ = load_character_history_manifest(
                legacy_path, data_root=data_root
            )
            self.assertEqual(
                ingest_v1.POSTFIX_KNOWN_CLEAN,
                loaded_legacy["characters"][0]["instances"][0][
                    "contamination_label"
                ],
            )

            object_root = (
                data_root / "chronicle_raw" / "external_api" / "v1" / "objects"
            )
            object_files_before = sorted(
                (path, hashlib.sha256(path.read_bytes()).hexdigest())
                for path in object_root.rglob("*")
                if path.is_file()
            )
            replay = history_v1.replay_character_history_manifest_from_local_raw(
                legacy_path, data_root=data_root
            )
            self.assertEqual(
                "LEGACY_REVISION_MIGRATED_TO_CURRENT", replay["status"]
            )
            self.assertEqual(0, replay["network_requests_made"])
            self.assertEqual(0, replay["raw_objects_written"])
            self.assertTrue(legacy_path.is_file())
            migrated, migrated_path = load_character_history_manifest(
                replay["manifest_path"], data_root=data_root
            )
            self.assertEqual(current, migrated)
            self.assertEqual(current_path, migrated_path)
            self.assertEqual(
                ingest_v1.SUSPECT_36YD_RANGE_BUG,
                migrated["characters"][0]["instances"][0][
                    "contamination_label"
                ],
            )
            object_files_after = sorted(
                (path, hashlib.sha256(path.read_bytes()).hexdigest())
                for path in object_root.rglob("*")
                if path.is_file()
            )
            self.assertEqual(object_files_before, object_files_after)
            self.assertEqual(
                [current_path],
                sync_v1.discover_character_manifests(
                    data_root, [_query()]
                ),
            )

    def test_history_snapshot_updates_are_retained_and_selected_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "offline_data"
            older_identity = _identity()
            older_identity["updated_at"] = "2026-09-10T00:00:00Z"
            older_log = _log(
                INSTANCE_A,
                slug="slug-a",
                started_at="2026-09-02T01:00:00Z",
                uploaded_at="2026-09-03T01:00:00Z",
            )
            older_log["performance"][0]["dps_parse"] = {"display_score": 91}
            older = _capture_fixture(
                data_root, logs=[older_log], identity=older_identity
            )

            newer_identity = deepcopy(older_identity)
            newer_identity["updated_at"] = "2026-09-11T00:00:00Z"
            newer_log = deepcopy(older_log)
            newer_log["performance"][0]["dps_parse"] = {"display_score": 97}
            newer = _capture_fixture(
                data_root, logs=[newer_log], identity=newer_identity
            )
            self.assertNotEqual(older["content_sha256"], newer["content_sha256"])

            first = build_character_instance_inventory(
                [older["manifest_path"], newer["manifest_path"]],
                [],
                data_root=data_root,
                required_streams=("damage",),
            )
            reversed_order = build_character_instance_inventory(
                [newer["manifest_path"], older["manifest_path"]],
                [],
                data_root=data_root,
                required_streams=("damage",),
            )
            self.assertEqual(first, reversed_order)
            membership = first["instances"][0]
            self.assertEqual(
                97, membership["performance"][0]["dps_parse"]["display_score"]
            )
            audit = membership["history_version_audit"]
            self.assertEqual(2, audit["version_count"])
            self.assertEqual(2, audit["distinct_membership_count"])
            self.assertEqual(
                newer["content_sha256"], audit["selected_source_manifest_sha256"]
            )
            self.assertEqual(1, sum(bool(row["selected"]) for row in audit["versions"]))
            self.assertEqual(
                {91, 97},
                {
                    row["membership"]["performance"][0]["dps_parse"][
                        "display_score"
                    ]
                    for row in audit["versions"]
                },
            )

    def test_inventory_publish_binary_utf8_replay_and_tamper_detection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "offline_data"
            capture = _capture_fixture(data_root)
            ingest = _write_ingest_manifest(
                data_root,
                [{"instance_id": INSTANCE_A, "slug": "slug-a", "streams": ["damage"]}],
            )
            manifest_directory = (
                data_root / Path(history_v1.INVENTORY_MANIFEST_DIRECTORY)
            )
            common_arguments = [
                "inventory",
                "--character-manifest",
                str(capture["manifest_path"]),
                "--ingest-manifest",
                str(ingest),
                "--data-root",
                str(data_root),
                "--required-stream",
                "damage",
            ]

            read_only_bytes = io.BytesIO()
            self.assertEqual(
                0,
                history_v1.main(common_arguments, stdout_buffer=read_only_bytes),
            )
            read_only = json.loads(read_only_bytes.getvalue().decode("utf-8"))
            self.assertEqual("托 尼牛", read_only["instances"][0]["character_name"])
            self.assertFalse(manifest_directory.exists())

            published_bytes = io.BytesIO()
            self.assertEqual(
                0,
                history_v1.main(
                    [*common_arguments, "--publish"],
                    stdout_buffer=published_bytes,
                ),
            )
            published = json.loads(published_bytes.getvalue().decode("utf-8"))
            published_path = Path(published["manifest_path"])
            self.assertTrue(published_path.is_file())
            repeated = publish_character_instance_inventory(
                [capture["manifest_path"]],
                [ingest],
                data_root=data_root,
                required_streams=("damage",),
            )
            self.assertEqual(published, repeated)
            loaded, resolved = load_character_instance_inventory_manifest(
                published_path, data_root=data_root
            )
            self.assertEqual(published_path, resolved)
            self.assertEqual(published["content_sha256"], loaded["content_address"]["sha256"])
            self.assertEqual(read_only, loaded)
            self.assertIn(
                "does not preserve",
                loaded["evidence_contract"]["upstream_ranking_endpoint_binding"][
                    "limitation"
                ],
            )

            original = published_path.read_bytes()
            published_path.write_bytes(original + b" ")
            with self.assertRaisesRegex(CharacterHistoryError, "canonical|hash"):
                load_character_instance_inventory_manifest(
                    published_path, data_root=data_root
                )
            published_path.write_bytes(original)

            tampered_core = {
                key: value
                for key, value in loaded.items()
                if key != "content_address"
            }
            tampered_core["summary"]["player_instance_count"] = 999
            tampered = history_v1._content_addressed(tampered_core)
            tampered_digest = tampered["content_address"]["sha256"]
            tampered_path = manifest_directory / (
                f"{history_v1.INVENTORY_MANIFEST_PREFIX}.{tampered_digest}.manifest.json"
            )
            ingest_v1._atomic_write_bytes(
                tampered_path, history_v1._canonical_document_bytes(tampered)
            )
            with self.assertRaisesRegex(CharacterHistoryError, "source-manifest replay"):
                load_character_instance_inventory_manifest(
                    tampered_path, data_root=data_root
                )

    def test_character_manifest_tamper_and_path_escape_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "offline_data"
            capture = _capture_fixture(data_root)
            path = Path(capture["manifest_path"])
            manifest = json.loads(path.read_text(encoding="utf-8"))
            ref = manifest["characters"][0]["identity_response"]["object"]
            raw_root = data_root / "chronicle_raw" / "external_api" / "v1"
            object_path = raw_root / ref["relative_path"]
            original = object_path.read_bytes()
            object_path.write_bytes(original + b"tamper")
            with self.assertRaisesRegex(CharacterHistoryError, "hash|size"):
                load_character_history_manifest(path, data_root=data_root)
            object_path.write_bytes(original)

            core = {
                key: value
                for key, value in manifest.items()
                if key != "content_address"
            }
            core["characters"][0]["identity_response"]["object"][
                "relative_path"
            ] = "../escape.json"
            rewritten = history_v1._content_addressed(core)
            payload = history_v1._canonical_document_bytes(rewritten)
            escaped_path = path.parent / (
                "chronicle_external_character_history_v1."
                f"{rewritten['content_address']['sha256']}.manifest.json"
            )
            ingest_v1._atomic_write_bytes(escaped_path, payload)
            with self.assertRaisesRegex(CharacterHistoryError, "escapes|safe relative"):
                load_character_history_manifest(escaped_path, data_root=data_root)

    def test_ingest_manifest_object_tamper_and_path_escape_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "offline_data"
            capture = _capture_fixture(data_root)
            ingest = _write_ingest_manifest(
                data_root,
                [{"instance_id": INSTANCE_A, "slug": "slug-a", "streams": []}],
            )
            raw_root = data_root / "chronicle_raw" / "external_api" / "v1"
            manifest = json.loads(ingest.read_text(encoding="utf-8"))
            ranking_ref = manifest["instances"][0]["ranking_records"]["object"]
            ranking_path = raw_root / ranking_ref["relative_path"]
            original = ranking_path.read_bytes()
            ranking_path.write_bytes(original + b"tamper")
            with self.assertRaisesRegex(CharacterHistoryError, "hash|size"):
                build_character_instance_inventory(
                    [capture["manifest_path"]], [ingest], data_root=data_root
                )
            ranking_path.write_bytes(original)

            manifest["instances"][0]["ranking_records"]["object"][
                "relative_path"
            ] = "../escape.json"
            payload = ingest_v1._canonical_json_bytes(manifest)
            escaped = ingest.parent / f"{hashlib.sha256(payload).hexdigest()}.json"
            ingest_v1._atomic_write_bytes(escaped, payload)
            with self.assertRaisesRegex(CharacterHistoryError, "escapes"):
                build_character_instance_inventory(
                    [capture["manifest_path"]], [escaped], data_root=data_root
                )

    def test_inventory_and_cli_are_deterministic_across_ingest_manifest_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "offline_data"
            logs = [
                _log(
                    INSTANCE_A,
                    slug="slug-a",
                    guild="Other Guild",
                    started_at="2026-09-02T00:00:00Z",
                    uploaded_at="2026-09-03T00:00:00Z",
                ),
                _log(
                    INSTANCE_D,
                    slug="slug-d",
                    guild="Other Guild",
                    started_at="2026-09-04T00:00:00Z",
                    uploaded_at="2026-09-05T00:00:00Z",
                ),
            ]
            capture = _capture_fixture(data_root, logs=logs)
            first_ingest = _write_ingest_manifest(
                data_root,
                [{"instance_id": INSTANCE_A, "slug": "slug-a", "streams": ["damage"]}],
            )
            second_ingest = _write_ingest_manifest(
                data_root,
                [{"instance_id": INSTANCE_D, "slug": "slug-d", "streams": ["damage"]}],
            )
            first = build_character_instance_inventory(
                [capture["manifest_path"]],
                [first_ingest, second_ingest],
                data_root=data_root,
                required_streams=("damage",),
            )
            second = build_character_instance_inventory(
                [capture["manifest_path"]],
                [second_ingest, first_ingest],
                data_root=data_root,
                required_streams=("damage",),
            )
            self.assertEqual(first, second)

            output = io.StringIO()
            exit_code = history_v1.main(
                [
                    "inventory",
                    "--character-manifest",
                    str(capture["manifest_path"]),
                    "--ingest-manifest",
                    str(first_ingest),
                    "--ingest-manifest",
                    str(second_ingest),
                    "--data-root",
                    str(data_root),
                    "--required-stream",
                    "damage",
                ],
                stdout=output,
            )
            self.assertEqual(0, exit_code)
            self.assertEqual(first, json.loads(output.getvalue()))

    def test_fetch_plan_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "offline_data"
            logs = [
                _log(
                    INSTANCE_A,
                    slug="slug-a",
                    guild="Other Guild",
                    started_at="2026-09-02T00:00:00Z",
                    uploaded_at="2026-09-03T00:00:00Z",
                ),
                _log(
                    INSTANCE_C,
                    slug="slug-c",
                    guild="Other Guild",
                    started_at="2026-09-04T00:00:00Z",
                    uploaded_at="2026-09-05T00:00:00Z",
                ),
            ]
            capture = _capture_fixture(data_root, logs=logs)
            with self.assertRaises(CharacterHistorySafetyLimitError):
                build_character_instance_inventory(
                    [capture["manifest_path"]],
                    [],
                    data_root=data_root,
                    max_fetch_instances=1,
                )


if __name__ == "__main__":
    unittest.main()
