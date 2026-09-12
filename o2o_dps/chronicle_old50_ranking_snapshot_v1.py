"""Freeze the 20 old50/Stage-6 Chronicle ranking-record responses.

This is deliberately a small raw-evidence capture.  It requests only the
public ``ranking-records`` JSON route for the frozen 20-instance overlap and
stores the exact response bytes in Chronicle's existing content-addressed raw
object store.  It never requests event streams, CSV exports, or checkpoints,
and it does not rebuild or mutate Stage 5/6.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Sequence, TextIO
from urllib.parse import quote

from . import chronicle_external_api_ingest_v1 as ingest_v1


SCHEMA = "chronicle_old50_ranking_snapshot/v1"
RECEIPT_SCHEMA = "chronicle_old50_ranking_snapshot_capture_receipt/v1"
REVISION = "ranking_records_only_exact_20_instance_capture_v1"
STATUS = "CAPTURED_RAW_IDENTITY_EVIDENCE_NONVOTING"
RECEIPT_STATUS = "COMPLETE"

STAGE5_MANIFEST_CONTENT_SHA256 = (
    "75fe0c215d09b6b0ca5e3a2c46b5397f70b4b5de60f8740dceae1b2465576bc0"
)
STAGE6_MANIFEST_CONTENT_SHA256 = (
    "8f5e00622ab45b89a3c989e7bab9a63ae59e11edcef4761b416349d0cfceeaf8"
)
OLD50_CAPSULE_CONTENT_SHA256 = (
    "23029ac5328e8c5e9e3009010e5d2876415d69b0e3d39e23e0ba4bf66a48e63e"
)
OVERLAP_PLAN_CONTENT_SHA256 = (
    "a0989f721ee18a75c38fc5622c19c35f17f3181bf08e707e0a185a2855617cfb"
)
OVERLAP_AUDIT_CONTENT_SHA256 = (
    "374d168d678385eec7d4ac21b2bac5eec165a7bf8edea3ffbe136b15fae9140d"
)

INSTANCE_IDS = (
    "043b4d65-9c58-4643-b601-62f1684af04a",
    "0dc00ded-ed32-4232-b307-f4a4e1dd81ba",
    "25581d5e-f03f-474c-8918-f111dd5da111",
    "271d7e96-f88d-4969-ae0a-76a8502db030",
    "30ed6d3f-b0d4-41bb-ab5b-c416b918eeca",
    "367f5371-3168-4f2c-81a6-47930ff7d67a",
    "52c5bea9-1bf4-4271-b8b1-00b48f2b4da2",
    "585a0077-3044-418b-8c30-49c0514e1015",
    "5ee051bc-2de5-45b9-a0e3-296ebb460d21",
    "83ba7fc8-c567-4a9a-bf73-4bfb5bebd69b",
    "83cc5a06-24a8-44de-996d-82763cdc6c5e",
    "a09db0c9-5480-41fa-ba77-28094a29c732",
    "a2f04daa-b640-44f2-bee2-69cb08197452",
    "a7e95b70-e8bf-4ba4-832e-547bc449c25c",
    "ae751904-9890-4d52-af24-d6e575c4f49a",
    "b65658d0-55cb-4cd1-b261-5e3c43c95281",
    "c19c3560-d7c4-4f93-a9ac-00555a6a6308",
    "cf84326b-77ba-4f07-a8f6-e58f494ee258",
    "dc175895-9524-421b-81a5-5d60ff698de6",
    "fc6074bb-b436-4635-8929-42b166c13b32",
)

_SHA_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_UUID_RE = re.compile(
    r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z"
)


class ChronicleOld50RankingSnapshotError(RuntimeError):
    """The bounded ranking capture or its durable closure is invalid."""


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ChronicleOld50RankingSnapshotError(
            f"value is not canonical JSON: {error}"
        ) from error


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _content_addressed(core: Mapping[str, Any]) -> dict[str, Any]:
    value = deepcopy(dict(core))
    value.pop("content_address", None)
    value["content_address"] = {
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": _sha256_bytes(_canonical_bytes(value)),
    }
    return value


def _verify_content_address(value: Mapping[str, Any], *, label: str) -> str:
    raw = deepcopy(dict(value))
    address = raw.pop("content_address", None)
    expected = {
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": _sha256_bytes(_canonical_bytes(raw)),
    }
    if address != expected:
        raise ChronicleOld50RankingSnapshotError(f"{label} content address differs")
    return expected["sha256"]


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise ChronicleOld50RankingSnapshotError("capture clock must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _header(headers: Mapping[str, str], name: str) -> str | None:
    lowered = name.casefold()
    for key, value in headers.items():
        if str(key).casefold() == lowered:
            return str(value)
    return None


def _manifest_bytes(value: Mapping[str, Any]) -> bytes:
    return _canonical_bytes(value) + b"\n"


def _relative_to_raw(path: Path, raw_root: Path) -> str:
    return path.resolve().relative_to(raw_root.resolve()).as_posix()


def _publish_document(
    value: Mapping[str, Any], *, directory: Path, prefix: str
) -> tuple[Path, str, int]:
    content_sha = _verify_content_address(value, label=prefix)
    data = _manifest_bytes(value)
    path = directory / f"{prefix}.{content_sha}.json"
    ingest_v1._atomic_write_bytes(path, data)
    return path, _sha256_bytes(data), len(data)


def capture_old50_ranking_snapshot(
    *,
    data_root: Path,
    client: ingest_v1.ChronicleClient | None = None,
    now: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Capture exactly the frozen 20 ranking endpoints and publish manifest-last."""

    clock = now or (lambda: datetime.now(timezone.utc))
    if tuple(sorted(INSTANCE_IDS)) != INSTANCE_IDS or len(set(INSTANCE_IDS)) != 20:
        raise ChronicleOld50RankingSnapshotError(
            "the frozen overlap instance population is not 20 unique sorted UUIDs"
        )
    for instance_id in INSTANCE_IDS:
        if _UUID_RE.fullmatch(instance_id) is None:
            raise ChronicleOld50RankingSnapshotError(
                f"invalid frozen instance UUID: {instance_id}"
            )

    api = client or ingest_v1.ChronicleClient(max_attempts=1)
    store = ingest_v1.RawObjectStore(Path(data_root))
    raw_root = store.root
    capture_started = _utc_text(clock())
    instances: list[dict[str, Any]] = []
    for instance_id in INSTANCE_IDS:
        encoded = quote(instance_id, safe="")
        path = f"/raidlogs/instances/{encoded}/ranking-records"
        expected_url = f"{api.api_base}{path}"
        request_started = _utc_text(clock())
        response, decoded = api.get_json(path)
        request_finished = _utc_text(clock())
        if response.status != 200:
            raise ChronicleOld50RankingSnapshotError(
                f"ranking response for {instance_id} was HTTP {response.status}"
            )
        if response.url != expected_url:
            raise ChronicleOld50RankingSnapshotError(
                f"ranking response URL differs for {instance_id}: {response.url}"
            )
        if not isinstance(decoded, list) or not all(
            isinstance(row, Mapping) for row in decoded
        ):
            raise ChronicleOld50RankingSnapshotError(
                f"ranking response for {instance_id} must be a JSON object array"
            )
        object_ref = store.put(
            response.body, suffix="json", media_type="application/json"
        )
        instances.append(
            {
                "instance_id": instance_id,
                "request": {
                    "method": "GET",
                    "endpoint": expected_url,
                    "query": {},
                    "accept": "application/json",
                    "started_at_utc": request_started,
                    "finished_at_utc": request_finished,
                },
                "response": {
                    "url": response.url,
                    "http_status": response.status,
                    "content_type": _header(response.headers, "content-type"),
                    "etag": _header(response.headers, "etag"),
                    "last_modified": _header(response.headers, "last-modified"),
                    "response_size_bytes": len(response.body),
                    "response_sha256": _sha256_bytes(response.body),
                    "record_count": len(decoded),
                    "object": object_ref,
                },
            }
        )
    capture_finished = _utc_text(clock())

    total_bytes = sum(row["response"]["response_size_bytes"] for row in instances)
    total_records = sum(row["response"]["record_count"] for row in instances)
    source_bindings = {
        "stage5_manifest_content_sha256": STAGE5_MANIFEST_CONTENT_SHA256,
        "stage6_manifest_content_sha256": STAGE6_MANIFEST_CONTENT_SHA256,
        "old50_capsule_content_sha256": OLD50_CAPSULE_CONTENT_SHA256,
        "overlap_plan_content_sha256": OVERLAP_PLAN_CONTENT_SHA256,
        "overlap_audit_content_sha256": OVERLAP_AUDIT_CONTENT_SHA256,
    }
    manifest = _content_addressed(
        {
            "schema": SCHEMA,
            "revision": REVISION,
            "kind": "chronicle_external_ranking_records_raw_snapshot",
            "status": STATUS,
            "source_bindings": source_bindings,
            "capture": {
                "started_at_utc": capture_started,
                "finished_at_utc": capture_finished,
            },
            "request_contract": {
                "api_base": api.api_base,
                "method": "GET",
                "endpoint_template": (
                    api.api_base
                    + "/raidlogs/instances/{instance_id}/ranking-records"
                ),
                "query": {},
                "accept": "application/json",
                "authentication": "none",
                "logical_request_count": 20,
                "event_stream_requests": 0,
                "csv_requests": 0,
                "checkpoint_requests": 0,
            },
            "instance_order": list(INSTANCE_IDS),
            "instances": instances,
            "summary": {
                "instance_count": len(instances),
                "successful_response_count": sum(
                    row["response"]["http_status"] == 200 for row in instances
                ),
                "ranking_record_count": total_records,
                "response_size_bytes": total_bytes,
                "etag_present_count": sum(
                    row["response"]["etag"] is not None for row in instances
                ),
                "last_modified_present_count": sum(
                    row["response"]["last_modified"] is not None
                    for row in instances
                ),
            },
            "scientific_boundary": {
                "raw_identity_and_spec_evidence_only": True,
                "stage5_or_stage6_mutated": False,
                "selection_or_outcome_evaluation_performed": False,
                "comparison_ready": False,
                "deployment_ready": False,
            },
        }
    )
    manifest_path, manifest_file_sha, manifest_size = _publish_document(
        manifest,
        directory=raw_root / "ranking_snapshot_manifests",
        prefix="chronicle_old50_ranking_snapshot_v1",
    )

    receipt = _content_addressed(
        {
            "schema": RECEIPT_SCHEMA,
            "revision": REVISION,
            "status": RECEIPT_STATUS,
            "operation": "RANKING_RECORDS_ONLY_EXACT_20_INSTANCE_CAPTURE",
            "capture": deepcopy(manifest["capture"]),
            "request_contract": deepcopy(manifest["request_contract"]),
            "source_bindings": source_bindings,
            "responses": [
                {
                    "instance_id": row["instance_id"],
                    "endpoint": row["request"]["endpoint"],
                    "query": {},
                    "started_at_utc": row["request"]["started_at_utc"],
                    "finished_at_utc": row["request"]["finished_at_utc"],
                    "http_status": row["response"]["http_status"],
                    "response_size_bytes": row["response"]["response_size_bytes"],
                    "response_sha256": row["response"]["response_sha256"],
                    "etag": row["response"]["etag"],
                    "last_modified": row["response"]["last_modified"],
                }
                for row in instances
            ],
            "publication": {
                "manifest_schema": SCHEMA,
                "manifest_content_sha256": manifest["content_address"]["sha256"],
                "manifest_file_sha256": manifest_file_sha,
                "manifest_size_bytes": manifest_size,
                "manifest_relative_path": _relative_to_raw(
                    manifest_path, raw_root
                ),
                "raw_object_count": len(
                    {
                        row["response"]["object"]["sha256"] for row in instances
                    }
                ),
                "manifest_published_after_all_raw_objects": True,
            },
            "summary": deepcopy(manifest["summary"]),
            "scientific_boundary": deepcopy(manifest["scientific_boundary"]),
        }
    )
    receipt_path, receipt_file_sha, receipt_size = _publish_document(
        receipt,
        directory=raw_root / "ranking_snapshot_receipts",
        prefix="chronicle_old50_ranking_snapshot_capture_receipt_v1",
    )
    return {
        "status": RECEIPT_STATUS,
        "manifest_path": str(manifest_path),
        "manifest_content_sha256": manifest["content_address"]["sha256"],
        "manifest_file_sha256": manifest_file_sha,
        "receipt_path": str(receipt_path),
        "receipt_content_sha256": receipt["content_address"]["sha256"],
        "receipt_file_sha256": receipt_file_sha,
        "receipt_size_bytes": receipt_size,
        "instance_count": len(instances),
        "ranking_record_count": total_records,
        "response_size_bytes": total_bytes,
        "network_request_count": len(instances),
        "event_stream_requests": 0,
        "csv_requests": 0,
        "checkpoint_requests": 0,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chronicle_old50_ranking_snapshot_v1",
        description=(
            "Capture only the 20 frozen old50/Stage-6 ranking-record routes"
        ),
    )
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--api-base", default=ingest_v1.EXTERNAL_API_BASE)
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument("--request-interval-seconds", type=float, default=1.05)
    return parser


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    args = build_parser().parse_args(argv)
    client = ingest_v1.ChronicleClient(
        api_base=args.api_base,
        timeout_seconds=args.timeout_seconds,
        max_attempts=1,
        request_interval_seconds=args.request_interval_seconds,
    )
    result = capture_old50_ranking_snapshot(
        data_root=args.data_root,
        client=client,
    )
    (stdout or __import__("sys").stdout).write(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "INSTANCE_IDS",
    "RECEIPT_SCHEMA",
    "REVISION",
    "SCHEMA",
    "capture_old50_ranking_snapshot",
]
