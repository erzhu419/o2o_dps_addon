from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.chronicle_export_assistant import (
    EXTERNAL_API_BASE,
    collect_pending,
    discover_instances,
    load_queue,
    matching_downloads,
    merge_discovery,
    merge_manifest,
    resolve_manifest_instances,
)


FIXTURE = Path(__file__).parent / "fixtures" / "all-activity-instance-fixture.csv"


def _source(character: str) -> dict[str, str]:
    return {
        "server": "Turtle WoW",
        "realm": "Nordanaar",
        "character": character,
    }


class ChronicleExportAssistantTests(unittest.TestCase):
    def test_discovery_paginates_and_url_encodes_character_path(self) -> None:
        requested_urls: list[str] = []

        def fake_get_json(url: str) -> dict[str, object]:
            requested_urls.append(url)
            if "page=1&page_size=50" in url:
                return {
                    "logs": [
                        {
                            "id": "instance-page-1",
                            "name": "Molten Core",
                            "started_at": "2026-08-01T00:00:00Z",
                        }
                    ],
                    "pagination": {"has_more": True},
                }
            return {
                "logs": [
                    {
                        "id": "instance-page-2",
                        "name": "Blackwing Lair",
                        "started_at": "2026-08-02T00:00:00Z",
                    }
                ],
                "pagination": {"has_more": False},
            }

        instances = discover_instances(
            [_source("Cat")],
            get_json=fake_get_json,
        )

        self.assertEqual(
            requested_urls,
            [
                f"{EXTERNAL_API_BASE}/characters/Turtle%20WoW/Nordanaar/Cat/instances?page=1&page_size=50",
                f"{EXTERNAL_API_BASE}/characters/Turtle%20WoW/Nordanaar/Cat/instances?page=2&page_size=50",
            ],
        )
        self.assertEqual(
            [entry["instance_id"] for entry in instances],
            ["instance-page-2", "instance-page-1"],
        )

    def test_discovery_deduplicates_same_instance_across_characters(self) -> None:
        def fake_get_json(url: str) -> dict[str, object]:
            character = "Cat" if "/Cat/instances" in url else "Contra"
            return {
                "logs": [
                    {
                        "id": "shared-instance",
                        "name": "Molten Core",
                        "started_at": "2026-08-02T00:00:00Z",
                    },
                    {
                        "id": f"only-{character.lower()}",
                        "name": "Onyxia",
                        "started_at": "2026-08-01T00:00:00Z",
                    },
                ],
                "pagination": {"has_more": False},
            }

        instances = discover_instances(
            [_source("Cat"), _source("Contra")],
            get_json=fake_get_json,
        )

        self.assertEqual(len(instances), 3)
        shared = next(
            entry for entry in instances if entry["instance_id"] == "shared-instance"
        )
        self.assertEqual(
            [source["character"] for source in shared["discovered_from"]],
            ["Cat", "Contra"],
        )

    def test_rediscovery_preserves_imported_state_and_receipt(self) -> None:
        old_source = _source("Cat")
        new_source = _source("Contra")
        receipt = {
            "status": "ok",
            "instance": "instance-1",
            "row_count": 17,
            "normalized": "normalized/instance-1.jsonl",
        }
        queue = {
            "schema_version": 1,
            "kind": "chronicle_manual_export_queue",
            "created_at": "2026-08-01T00:00:00Z",
            "updated_at": "2026-08-01T00:00:00Z",
            "characters": [old_source],
            "entries": [
                {
                    "instance_id": "instance-1",
                    "name": "Old name",
                    "started_at": "2026-08-01T00:00:00Z",
                    "discovered_from": [old_source],
                    "status": "imported",
                    "imported_at": "2026-08-03T00:00:00Z",
                    "downloaded_file": "Downloads/all-activity-instance-1.csv",
                    "import_receipt": receipt,
                }
            ],
        }
        rediscovered = {
            "instance_id": "instance-1",
            "name": "Canonical name",
            "started_at": "2026-08-01T00:00:00Z",
            "discovered_from": [new_source],
            "status": "pending",
        }

        merged, added = merge_discovery(queue, [rediscovered], [new_source])

        self.assertEqual(added, 0)
        entry = merged["entries"][0]
        self.assertEqual(entry["name"], "Canonical name")
        self.assertEqual(entry["status"], "imported")
        self.assertEqual(entry["imported_at"], "2026-08-03T00:00:00Z")
        self.assertEqual(entry["import_receipt"], receipt)
        self.assertEqual(
            [source["character"] for source in entry["discovered_from"]],
            ["Cat", "Contra"],
        )

    def test_external_discovery_merges_pdf_slug_alias_without_duplicate(self) -> None:
        leaderboard_row = {
            "source_file": "board.pdf",
            "source_page": 1,
            "rank": 1,
            "character": "Cat",
            "instance_slug": "route-slug",
        }
        queue = {
            "schema_version": 1,
            "kind": "chronicle_manual_export_queue",
            "created_at": "2026-08-01T00:00:00Z",
            "updated_at": "2026-08-01T00:00:00Z",
            "characters": [],
            "entries": [
                {
                    "instance_id": "route-slug",
                    "slug": "route-slug",
                    "discovered_from": [],
                    "leaderboard_rows": [leaderboard_row],
                    "status": "pending",
                }
            ],
        }
        discovered = {
            "instance_id": "instance-uuid",
            "slug": "route-slug",
            "name": "Upper Tower of Karazhan",
            "discovered_from": [_source("Cat")],
            "status": "pending",
        }

        merged, added = merge_discovery(queue, [discovered], [_source("Cat")])

        self.assertEqual(added, 0)
        self.assertEqual(len(merged["entries"]), 1)
        self.assertEqual(merged["entries"][0]["instance_id"], "instance-uuid")
        self.assertEqual(merged["entries"][0]["slug"], "route-slug")
        self.assertEqual(merged["entries"][0]["leaderboard_rows"], [leaderboard_row])

    def test_manifest_slugs_resolve_through_one_cached_character_history(self) -> None:
        requested_urls: list[str] = []

        def fake_get_json(url: str) -> dict[str, object]:
            requested_urls.append(url)
            return {
                "logs": [
                    {"id": "uuid-1", "slug": "slug-1", "name": "Upper Tower"},
                    {"id": "uuid-2", "slug": "slug-2", "name": "Upper Tower"},
                ],
                "pagination": {"has_more": False},
            }

        manifest = {
            "server": "Capybara",
            "entries": [
                {
                    "instance_slug": "slug-1",
                    "character": "Same Player",
                    "realm": "Basin of Stars",
                },
                {
                    "instance_slug": "slug-2",
                    "character": "Same Player",
                    "realm": "Basin of Stars",
                },
            ],
        }

        instances, sources, unresolved = resolve_manifest_instances(
            manifest, get_json=fake_get_json
        )

        self.assertEqual(len(requested_urls), 1)
        self.assertIn("Same%20Player", requested_urls[0])
        self.assertEqual(
            [(entry["instance_id"], entry["slug"]) for entry in instances],
            [("uuid-1", "slug-1"), ("uuid-2", "slug-2")],
        )
        self.assertEqual(sources[0]["character"], "Same Player")
        self.assertEqual(unresolved, [])

    def test_matching_downloads_accepts_windows_duplicate_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            downloads = Path(temporary_directory)
            expected = downloads / "all-activity-instance-1 (1).csv"
            expected.write_text("download", encoding="utf-8")
            (downloads / "all-activity-instance-10 (1).csv").write_text(
                "other instance", encoding="utf-8"
            )
            (downloads / "all-activity-instance-1 (1).csv.crdownload").write_text(
                "partial", encoding="utf-8"
            )

            matches = matching_downloads(downloads, "instance-1")

            self.assertEqual(matches, [expected])

    def test_matching_downloads_accepts_exact_slug_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            downloads = Path(temporary_directory)
            expected = downloads / "all-activity-route-slug_ (2).csv"
            expected.write_text("download", encoding="utf-8")
            (downloads / "all-activity-route-slug_extra.csv").write_text(
                "similar but different", encoding="utf-8"
            )

            matches = matching_downloads(
                downloads,
                "instance-uuid",
                ["route-slug_"],
            )

            self.assertEqual(matches, [expected])

    def test_manifest_merge_is_idempotent_and_keeps_import_state(self) -> None:
        receipt = {"status": "ok", "row_count": 100}
        existing = {
            "schema_version": 1,
            "kind": "chronicle_manual_export_queue",
            "created_at": "2026-08-01T00:00:00Z",
            "updated_at": "2026-08-01T00:00:00Z",
            "characters": [],
            "entries": [
                {
                    "instance_id": "instance-uuid",
                    "slug": "shared-slug-",
                    "name": "Upper Tower of Karazhan",
                    "page_url": "https://capy.chronicleclassic.com/instances/shared-slug-",
                    "export_url": "https://capy.chronicleclassic.com/instances/shared-slug-",
                    "discovered_from": [],
                    "status": "imported",
                    "import_receipt": receipt,
                }
            ],
        }
        row = {
            "source_file": "board.pdf",
            "source_page": 1,
            "board_class": "WARRIOR",
            "board_spec": "Fury",
            "rank": 1,
            "character": "Narcissly",
            "observed_spec": "Fury",
            "realm": "Basin of Stars",
            "dps": 1479,
            "raid_date": "2026-08-26",
            "instance_slug": "shared-slug-",
            "instance_url": "https://capy.chronicleclassic.com/instances/shared-slug-",
        }
        manifest = {
            "generated_at": "2026-08-28T00:00:00Z",
            "selection_policy": "all_rows_in_supplied_pdf_prints",
            "raid": "Upper Tower of Karazhan",
            "summary": {"leaderboard_row_count": 1, "unique_instance_count": 1},
            "entries": [row],
        }

        merged, added = merge_manifest(
            existing, manifest, manifest_path="leaderboard_manifest.json"
        )
        merged_again, added_again = merge_manifest(
            merged, manifest, manifest_path="leaderboard_manifest.json"
        )

        self.assertEqual(added, 0)
        self.assertEqual(added_again, 0)
        self.assertEqual(len(merged_again["entries"]), 1)
        entry = merged_again["entries"][0]
        self.assertEqual(entry["instance_id"], "instance-uuid")
        self.assertEqual(entry["status"], "imported")
        self.assertEqual(entry["import_receipt"], receipt)
        self.assertEqual(entry["leaderboard_rows"], [row])
        self.assertEqual(len(merged_again["leaderboard_manifests"]), 1)

    def test_existing_fixture_is_imported_and_queue_is_checkpointed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            downloads = root / "Downloads"
            downloads.mkdir()
            downloaded_csv = downloads / "all-activity-instance-auto.csv"
            shutil.copyfile(FIXTURE, downloaded_csv)
            data_root = root / "offline_data"
            queue = {
                "schema_version": 1,
                "kind": "chronicle_manual_export_queue",
                "created_at": "2026-08-01T00:00:00Z",
                "updated_at": "2026-08-01T00:00:00Z",
                "characters": [_source("Cat")],
                "entries": [
                    {
                        "instance_id": "instance-auto",
                        "name": "Molten Core",
                        "export_url": "https://capy.chronicleclassic.com/instances/instance-auto",
                        "discovered_from": [_source("Cat")],
                        "status": "pending",
                    }
                ],
            }

            with redirect_stdout(io.StringIO()):
                completed = collect_pending(
                    queue,
                    downloads=downloads,
                    data_root=data_root,
                    open_browser=False,
                    accept_existing=True,
                )

            self.assertEqual(completed, 1)
            entry = queue["entries"][0]
            self.assertEqual(entry["status"], "imported")
            self.assertEqual(entry["downloaded_file"], str(downloaded_csv.resolve()))
            self.assertEqual(entry["import_receipt"]["instance"], "instance-auto")
            self.assertEqual(entry["import_receipt"]["row_count"], 3)
            self.assertIn("not machine-verifiable", entry["event_stream_selection"])
            self.assertTrue(Path(entry["import_receipt"]["normalized"]).is_file())

            persisted = load_queue(data_root)
            self.assertEqual(persisted["entries"][0]["status"], "imported")
            self.assertEqual(
                persisted["entries"][0]["import_receipt"]["row_count"], 3
            )
            provenance = json.loads(
                Path(entry["import_receipt"]["metadata"]).read_text(encoding="utf-8")
            )
            self.assertEqual(provenance["instance"], "instance-auto")
            self.assertEqual(provenance["row_count"], 3)

    def test_preexisting_csv_is_not_silently_accepted_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            downloads = root / "Downloads"
            downloads.mkdir()
            shutil.copyfile(
                FIXTURE,
                downloads / "all-activity-instance-old.csv",
            )
            data_root = root / "offline_data"
            queue = {
                "schema_version": 1,
                "kind": "chronicle_manual_export_queue",
                "created_at": "2026-08-01T00:00:00Z",
                "updated_at": "2026-08-01T00:00:00Z",
                "characters": [_source("Cat")],
                "entries": [
                    {
                        "instance_id": "instance-old",
                        "name": "Old export",
                        "export_url": "https://capy.chronicleclassic.com/instances/instance-old",
                        "discovered_from": [_source("Cat")],
                        "status": "pending",
                    }
                ],
            }

            with redirect_stdout(io.StringIO()):
                completed = collect_pending(
                    queue,
                    downloads=downloads,
                    data_root=data_root,
                    open_browser=False,
                    timeout_minutes=0.000001,
                    poll_seconds=0,
                )

            self.assertEqual(completed, 0)
            self.assertEqual(queue["entries"][0]["status"], "pending")
            self.assertFalse((data_root / "normalized").exists())

    def test_csv_created_after_collection_starts_is_imported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            downloads = root / "Downloads"
            downloads.mkdir()
            data_root = root / "offline_data"
            queue = {
                "schema_version": 1,
                "kind": "chronicle_manual_export_queue",
                "created_at": "2026-08-01T00:00:00Z",
                "updated_at": "2026-08-01T00:00:00Z",
                "characters": [_source("Cat")],
                "entries": [
                    {
                        "instance_id": "instance-new",
                        "name": "New export",
                        "export_url": "https://capy.chronicleclassic.com/instances/instance-new",
                        "discovered_from": [_source("Cat")],
                        "status": "pending",
                    }
                ],
            }

            def create_download() -> None:
                time.sleep(0.02)
                shutil.copyfile(
                    FIXTURE,
                    downloads / "all-activity-instance-new.csv",
                )

            writer = threading.Thread(target=create_download)
            writer.start()
            try:
                with redirect_stdout(io.StringIO()):
                    completed = collect_pending(
                        queue,
                        downloads=downloads,
                        data_root=data_root,
                        open_browser=False,
                        timeout_minutes=0.1,
                        poll_seconds=0.005,
                    )
            finally:
                writer.join()

            self.assertEqual(completed, 1)
            self.assertEqual(queue["entries"][0]["status"], "imported")

    def test_unresolved_slug_accepts_only_new_official_uuid_csv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            downloads = root / "Downloads"
            downloads.mkdir()
            old_uuid = "11111111-1111-1111-1111-111111111111"
            new_uuid = "22222222-2222-2222-2222-222222222222"
            shutil.copyfile(
                FIXTURE,
                downloads / f"all-activity-{old_uuid}.csv",
            )
            data_root = root / "offline_data"
            queue = {
                "schema_version": 1,
                "kind": "chronicle_manual_export_queue",
                "created_at": "2026-08-01T00:00:00Z",
                "updated_at": "2026-08-01T00:00:00Z",
                "characters": [],
                "entries": [
                    {
                        "instance_id": "route-slug",
                        "slug": "route-slug",
                        "name": "Unresolved UUID",
                        "export_url": "https://capy.chronicleclassic.com/instances/route-slug",
                        "discovered_from": [],
                        "uuid_resolution": "unresolved_external_api",
                        "status": "pending",
                    }
                ],
            }

            def create_download() -> None:
                time.sleep(0.02)
                shutil.copyfile(
                    FIXTURE,
                    downloads / f"all-activity-{new_uuid}.csv",
                )

            writer = threading.Thread(target=create_download)
            writer.start()
            try:
                with redirect_stdout(io.StringIO()):
                    completed = collect_pending(
                        queue,
                        downloads=downloads,
                        data_root=data_root,
                        open_browser=False,
                        timeout_minutes=0.1,
                        poll_seconds=0.005,
                    )
            finally:
                writer.join()

            self.assertEqual(completed, 1)
            entry = queue["entries"][0]
            self.assertEqual(entry["status"], "imported")
            self.assertEqual(entry["download_instance_id"], new_uuid)
            self.assertTrue(entry["downloaded_file"].endswith(f"{new_uuid}.csv"))


if __name__ == "__main__":
    unittest.main()
