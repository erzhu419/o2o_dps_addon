from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps import chronicle_external_api_ingest_v1 as ingest_v1
from o2o_dps import chronicle_old50_ranking_snapshot_v1 as snapshot_v1


class _Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 12, 1, 2, 3, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        value = self.value
        self.value += timedelta(microseconds=1)
        return value


class _FakeClient:
    def __init__(self, *, wrong_url: bool = False, invalid_array: bool = False) -> None:
        self.api_base = ingest_v1.EXTERNAL_API_BASE
        self.calls: list[str] = []
        self.bodies: dict[str, bytes] = {}
        self.wrong_url = wrong_url
        self.invalid_array = invalid_array

    def get_json(self, path: str):
        self.calls.append(path)
        instance_id = path.split("/")[3]
        decoded = (
            {"not": "an array"}
            if self.invalid_array
            else [
                {
                    "id": f"ranking-{instance_id}",
                    "encounter_id": None,
                    "player_guid": "0x0000000000000001",
                    "player_class": "WARRIOR",
                    "player_spec": "Fury",
                    "player_role": "dps",
                }
            ]
        )
        body = json.dumps(decoded, separators=(",", ":")).encode("utf-8")
        self.bodies[instance_id] = body
        url = self.api_base + path
        if self.wrong_url:
            url += "?redirected=true"
        return (
            ingest_v1.HTTPResponse(
                status=200,
                headers={"content-type": "application/json; charset=utf-8"},
                body=body,
                url=url,
            ),
            decoded,
        )


class ChronicleOld50RankingSnapshotV1Tests(unittest.TestCase):
    def test_capture_publishes_raw_objects_manifest_and_receipt(self) -> None:
        client = _FakeClient()
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "offline_data"
            result = snapshot_v1.capture_old50_ranking_snapshot(
                data_root=data_root,
                client=client,
                now=_Clock(),
            )

            self.assertEqual(20, result["network_request_count"])
            self.assertEqual(20, result["instance_count"])
            self.assertEqual(20, result["ranking_record_count"])
            self.assertEqual(0, result["event_stream_requests"])
            self.assertEqual(0, result["csv_requests"])
            self.assertEqual(0, result["checkpoint_requests"])
            self.assertEqual(
                [
                    f"/raidlogs/instances/{instance_id}/ranking-records"
                    for instance_id in snapshot_v1.INSTANCE_IDS
                ],
                client.calls,
            )

            manifest_path = Path(result["manifest_path"])
            receipt_path = Path(result["receipt_path"])
            self.assertTrue(manifest_path.is_file())
            self.assertTrue(receipt_path.is_file())
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(snapshot_v1.SCHEMA, manifest["schema"])
            self.assertEqual(snapshot_v1.RECEIPT_SCHEMA, receipt["schema"])
            self.assertEqual(
                result["manifest_content_sha256"],
                snapshot_v1._verify_content_address(manifest, label="manifest"),
            )
            self.assertEqual(
                result["receipt_content_sha256"],
                snapshot_v1._verify_content_address(receipt, label="receipt"),
            )
            self.assertEqual(
                result["manifest_file_sha256"],
                hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                result["receipt_file_sha256"],
                hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(0, manifest["summary"]["etag_present_count"])
            self.assertEqual(0, manifest["summary"]["last_modified_present_count"])
            self.assertFalse(
                manifest["scientific_boundary"]["stage5_or_stage6_mutated"]
            )
            self.assertFalse(manifest["scientific_boundary"]["comparison_ready"])
            self.assertEqual(
                snapshot_v1.OVERLAP_AUDIT_CONTENT_SHA256,
                manifest["source_bindings"]["overlap_audit_content_sha256"],
            )

            raw_root = (
                data_root / "chronicle_raw" / "external_api" / "v1"
            )
            for row in manifest["instances"]:
                instance_id = row["instance_id"]
                response = row["response"]
                expected_body = client.bodies[instance_id]
                self.assertEqual({}, row["request"]["query"])
                self.assertEqual(200, response["http_status"])
                self.assertIsNone(response["etag"])
                self.assertIsNone(response["last_modified"])
                self.assertEqual(len(expected_body), response["response_size_bytes"])
                self.assertEqual(
                    hashlib.sha256(expected_body).hexdigest(),
                    response["response_sha256"],
                )
                object_path = raw_root / response["object"]["relative_path"]
                self.assertEqual(expected_body, object_path.read_bytes())
                self.assertEqual(
                    response["response_sha256"], response["object"]["sha256"]
                )
            self.assertTrue(
                receipt["publication"][
                    "manifest_published_after_all_raw_objects"
                ]
            )

    def test_redirected_response_url_is_rejected_before_publication(self) -> None:
        client = _FakeClient(wrong_url=True)
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "offline_data"
            with self.assertRaisesRegex(
                snapshot_v1.ChronicleOld50RankingSnapshotError,
                "response URL differs",
            ):
                snapshot_v1.capture_old50_ranking_snapshot(
                    data_root=data_root,
                    client=client,
                    now=_Clock(),
                )
            raw_root = data_root / "chronicle_raw" / "external_api" / "v1"
            self.assertFalse((raw_root / "ranking_snapshot_manifests").exists())
            self.assertFalse((raw_root / "ranking_snapshot_receipts").exists())

    def test_non_array_response_is_rejected_before_publication(self) -> None:
        client = _FakeClient(invalid_array=True)
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "offline_data"
            with self.assertRaisesRegex(
                snapshot_v1.ChronicleOld50RankingSnapshotError,
                "JSON object array",
            ):
                snapshot_v1.capture_old50_ranking_snapshot(
                    data_root=data_root,
                    client=client,
                    now=_Clock(),
                )
            raw_root = data_root / "chronicle_raw" / "external_api" / "v1"
            self.assertFalse((raw_root / "ranking_snapshot_manifests").exists())
            self.assertFalse((raw_root / "ranking_snapshot_receipts").exists())


if __name__ == "__main__":
    unittest.main()
