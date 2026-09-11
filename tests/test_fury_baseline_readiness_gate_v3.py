from __future__ import annotations

from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.fury_baseline_readiness_gate_v3 import (
    CAT2_BLOCKERS,
    CAT2_NONVOTING_POLICY,
    CAT_BLOCKERS,
    CAT_POLICY,
    CONTRA260817_BLOCKERS,
    CONTRA260817_POLICY,
    CONTRA_DEPLOYED_BLOCKERS,
    CONTRA_DEPLOYED_POLICY,
    CONTENT_ADDRESS_ALGORITHM,
    DEFAULT_CAT2_ROOT,
    DEFAULT_CAT_ROOT,
    DEFAULT_CONTRA260817_ROOT,
    DEFAULT_CONTRA_DEPLOYED_ROOT,
    DEFAULT_PROTOCOL,
    DEFAULT_RUNTIME_SNAPSHOT,
    EXPECTED_PROTOCOL_FILE_SHA256,
    GATE_ID,
    READINESS_FIELDS,
    REQUIRED_BASELINE_IDS,
    SCHEMA,
    FuryBaselineReadinessGateV3Error,
    build_readiness_report,
    main,
    serialize_readiness_report,
    validate_readiness_report,
)


def _by_id(rows: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {str(row["policy_id"]): row for row in rows}


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _rehash(report: dict[str, object]) -> None:
    core = {key: value for key, value in report.items() if key != "content_address"}
    report["content_address"] = {
        "algorithm": CONTENT_ADDRESS_ALGORITHM,
        "scope": "canonical JSON document excluding content_address",
        "sha256": hashlib.sha256(_canonical_bytes(core)).hexdigest(),
    }


class _BinaryStdout:
    def __init__(self) -> None:
        self.buffer = io.BytesIO()

    def write(self, value: str) -> int:
        return len(value)

    def flush(self) -> None:
        return None


@unittest.skipUnless(
    DEFAULT_RUNTIME_SNAPSHOT.is_file(),
    "local content-addressed runtime snapshot is not published",
)
class FuryBaselineReadinessGateV3Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.report = build_readiness_report(verify_live_sources=False)
        self.required = _by_id(self.report["required_baselines"])
        self.nonvoting = _by_id(self.report["nonvoting_sources"])

    def test_exact_protocol_membership_and_summary_remain_blocked(self) -> None:
        self.assertEqual(self.report["schema"], SCHEMA)
        self.assertEqual(self.report["gate_id"], GATE_ID)
        self.assertEqual(
            tuple(self.report["protocol"]["required_baseline_ids"]),
            REQUIRED_BASELINE_IDS,
        )
        self.assertEqual(
            tuple(row["policy_id"] for row in self.report["required_baselines"]),
            REQUIRED_BASELINE_IDS,
        )
        self.assertEqual(
            tuple(row["policy_id"] for row in self.report["nonvoting_sources"]),
            (CAT2_NONVOTING_POLICY,),
        )
        self.assertEqual(
            self.report["protocol"]["file_sha256"],
            EXPECTED_PROTOCOL_FILE_SHA256,
        )
        self.assertTrue(self.report["runtime_identity"]["strictly_verified"])
        self.assertFalse(
            self.report["runtime_identity"]["source_execution_observed"]
        )
        self.assertFalse(
            self.report["runtime_identity"]["comparison_eligible_by_itself"]
        )
        self.assertEqual(
            self.report["summary"],
            {
                "status": "BLOCKED",
                "required_baseline_count": 3,
                "comparison_ready_baseline_count": 0,
                "all_required_baselines_ready": False,
                "comparison_allowed": False,
                "nonvoting_sources_can_vote": False,
            },
        )

    def test_cat_source_and_legacy_profile_declaration_do_not_close_evidence(self) -> None:
        cat = self.required[CAT_POLICY]
        self.assertTrue(cat["authority"]["strictly_verified"])
        self.assertTrue(cat["profile_declaration_present"])
        self.assertTrue(cat["runtime_snapshot_bound"])
        self.assertEqual(set(cat["readiness"]), set(READINESS_FIELDS))
        self.assertTrue(cat["readiness"]["policy_profile"])
        for field in READINESS_FIELDS:
            if field != "policy_profile":
                self.assertFalse(cat["readiness"][field])
        for blocker in CAT_BLOCKERS:
            self.assertIn(blocker, cat["blockers"])
        self.assertFalse(cat["comparison_eligible"])

    def test_contra_deployed_v2_profile_is_not_runtime_or_full_adapter(self) -> None:
        contra = self.required[CONTRA_DEPLOYED_POLICY]
        self.assertTrue(contra["profile_declaration_present"])
        self.assertTrue(contra["runtime_snapshot_bound"])
        self.assertTrue(contra["readiness"]["policy_profile"])
        for field in READINESS_FIELDS:
            if field != "policy_profile":
                self.assertFalse(contra["readiness"][field])
        for blocker in CONTRA_DEPLOYED_BLOCKERS:
            self.assertIn(blocker, contra["blockers"])
        self.assertTrue(contra["source_order_model_available"])
        self.assertFalse(contra["readiness"]["ordered_sink_trace"])
        self.assertFalse(contra["comparison_eligible"])

    def test_contra260817_package_identity_is_not_a_comparable_policy(self) -> None:
        contra = self.required[CONTRA260817_POLICY]
        self.assertFalse(contra["profile_declaration_present"])
        self.assertFalse(contra["runtime_snapshot_bound"])
        self.assertTrue(all(value is False for value in contra["readiness"].values()))
        for blocker in CONTRA260817_BLOCKERS:
            self.assertIn(blocker, contra["blockers"])
        self.assertFalse(contra["comparison_eligible"])

    def test_cat2_action_plan_is_capability_only_and_cannot_vote(self) -> None:
        cat2 = self.nonvoting[CAT2_NONVOTING_POLICY]
        self.assertEqual(cat2["role"], "NONVOTING_CAPABILITY_SOURCE")
        self.assertEqual(cat2["action_plan_status"], "PLAN_ONLY_NOT_DISTILLED")
        self.assertFalse(cat2["runtime_snapshot_bound"])
        self.assertTrue(cat2["source_order_model_available"])
        self.assertFalse(cat2["readiness"]["ordered_sink_trace"])
        self.assertTrue(all(value is False for value in cat2["readiness"].values()))
        for blocker in CAT2_BLOCKERS:
            self.assertIn(blocker, cat2["blockers"])
        self.assertFalse(cat2["comparison_eligible"])
        self.assertFalse(self.report["summary"]["nonvoting_sources_can_vote"])

    def test_skipping_live_verification_is_an_explicit_additional_blocker(self) -> None:
        for entry in [*self.report["required_baselines"], *self.report["nonvoting_sources"]]:
            self.assertEqual(entry["source_identity"]["status"], "NOT_REQUESTED")
            self.assertIn("SOURCE_IDENTITY_NOT_LIVE_ATTESTED", entry["blockers"])
            self.assertFalse(entry["comparison_eligible"])

    @unittest.skipUnless(
        all(
            path.is_dir()
            for path in (
                DEFAULT_CAT_ROOT,
                DEFAULT_CONTRA_DEPLOYED_ROOT,
                DEFAULT_CONTRA260817_ROOT,
                DEFAULT_CAT2_ROOT,
            )
        ),
        "all four local source trees are required for live verification",
    )
    def test_live_source_identity_passes_without_promoting_any_entry(self) -> None:
        report = build_readiness_report()
        entries = [*report["required_baselines"], *report["nonvoting_sources"]]
        self.assertTrue(
            all(entry["source_identity"]["status"] == "PASS" for entry in entries)
        )
        self.assertTrue(all(entry["source_identity"]["facts"] for entry in entries))
        self.assertTrue(all(entry["comparison_eligible"] is False for entry in entries))
        self.assertFalse(report["summary"]["comparison_allowed"])

    def test_live_source_failure_is_reported_and_remains_fail_closed(self) -> None:
        report = build_readiness_report(
            cat_root=PROJECT_ROOT / "definitely-not-cat",
            verify_live_sources=True,
        )
        cat = _by_id(report["required_baselines"])[CAT_POLICY]
        self.assertEqual(cat["source_identity"]["status"], "FAIL")
        self.assertIn("SOURCE_IDENTITY_LIVE_VERIFICATION_FAILED", cat["blockers"])
        self.assertFalse(cat["comparison_eligible"])
        self.assertFalse(report["summary"]["comparison_allowed"])

    def test_manifest_or_protocol_byte_drift_is_fatal_not_a_soft_blocker(self) -> None:
        protocol = json.loads(DEFAULT_PROTOCOL.read_text(encoding="utf-8"))
        protocol["baseline_contract"]["required_baselines"][0][
            "comparison_eligible"
        ] = True
        with tempfile.TemporaryDirectory() as raw_tmp:
            path = Path(raw_tmp) / "protocol.json"
            path.write_text(json.dumps(protocol), encoding="utf-8")
            with self.assertRaisesRegex(
                FuryBaselineReadinessGateV3Error, "SHA-256 mismatch"
            ):
                build_readiness_report(
                    protocol_path=path,
                    verify_live_sources=False,
                )

    def test_content_address_and_fixed_v3_readiness_reject_tampering(self) -> None:
        stale = deepcopy(self.report)
        stale["required_baselines"][0]["readiness"]["runtime_load"] = True
        with self.assertRaisesRegex(
            FuryBaselineReadinessGateV3Error, "content address mismatch"
        ):
            validate_readiness_report(stale)

        rehashed = deepcopy(stale)
        _rehash(rehashed)
        with self.assertRaisesRegex(FuryBaselineReadinessGateV3Error, "readiness mismatch"):
            validate_readiness_report(rehashed)

        removed_blocker = deepcopy(self.report)
        removed_blocker["required_baselines"][0]["blockers"].pop(0)
        _rehash(removed_blocker)
        with self.assertRaisesRegex(FuryBaselineReadinessGateV3Error, "blockers mismatch"):
            validate_readiness_report(removed_blocker)

        promoted_contract = deepcopy(self.report)
        promoted_contract["readiness_contract"][
            "source_order_model_is_ordered_sink_trace"
        ] = True
        _rehash(promoted_contract)
        with self.assertRaisesRegex(
            FuryBaselineReadinessGateV3Error, "readiness contract mismatch"
        ):
            validate_readiness_report(promoted_contract)

    def test_report_is_deterministic_compact_canonical_json(self) -> None:
        first = build_readiness_report(verify_live_sources=False)
        second = build_readiness_report(verify_live_sources=False)
        self.assertEqual(first, second)
        payload = serialize_readiness_report(first)
        self.assertTrue(payload.endswith(b"\n"))
        self.assertNotIn(b'": ', payload)
        self.assertEqual(json.loads(payload), first)
        self.assertEqual(validate_readiness_report(first), first)

    def test_cli_emits_report_and_optionally_returns_blocked_code(self) -> None:
        stdout = _BinaryStdout()
        with patch("sys.stdout", stdout):
            code = main(["--skip-live-source-verification", "--require-ready"])
        self.assertEqual(code, 3)
        payload = stdout.buffer.getvalue()
        self.assertTrue(payload.endswith(b"\n"))
        self.assertNotIn(b'": ', payload)
        report = json.loads(payload)
        self.assertEqual(report["schema"], SCHEMA)
        self.assertFalse(report["summary"]["comparison_allowed"])


if __name__ == "__main__":
    unittest.main()
