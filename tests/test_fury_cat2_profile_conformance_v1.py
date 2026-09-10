from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps.fury_cat2_profile_conformance_v1 import (
    FuryCat2ProfileConformanceError,
    build_fury_cat2_profile_conformance,
    materialize_fury_cat2_profile_conformance,
)


SESSION = "shadow-test-session"


def _state(
    rage: float,
    *,
    bloodrage: float,
    bloodthirst: float,
    whirlwind: float,
    nearby: int = 1,
) -> dict[str, object]:
    return {
        "rage": rage,
        "targetPercentHealth": 52.0,
        "targetExists": True,
        "inCombat": True,
        "bloodrageCooldown": bloodrage,
        "bloodrageCooldownKnown": True,
        "bloodthirstCooldown": bloodthirst,
        "bloodthirstCooldownKnown": True,
        "whirlwindCooldown": whirlwind,
        "whirlwindCooldownKnown": True,
        "nearbyEnemies": nearby,
        "nearbyEnemiesKnown": True,
    }


def _actual(
    *,
    off_gcd: list[str] | None = None,
    queue: list[str] | None = None,
    gcd: list[str] | None = None,
) -> dict[str, list[str]]:
    return {
        "off_gcd": list(off_gcd or []),
        "queue": list(queue or []),
        "gcd": list(gcd or []),
    }


def _row(
    sequence: int,
    state: dict[str, object],
    actual: dict[str, list[str]],
) -> dict[str, object]:
    return {
        "schema": "fury_shadow_transition_fragment/v1",
        "identity": {
            "export_session_id": SESSION,
            "decision_id": f"decision-{sequence}",
            "sequence_in_session": sequence,
        },
        "state_before": state,
        "actual": {
            "source": "Cat2_exact_sink_trace",
            "executor": {
                "kind": "cat2_configuration_card_stack",
                "expert": "Cat2",
                "entry": "Cat2.ExecuteConfiguration",
                "profile_id": 1,
                "profile_name": "BrainOfCat Shadow",
                "executed_policy_id": None,
                "executed_policy_proposal_available": False,
            },
            "factorized_action": actual,
        },
        "eligibility": {
            "behavior_label_eligible": True,
            "offline_rl_episode_eligible": False,
            "deployment_allowed": False,
        },
        "provenance": {
            "kind": "OBSERVED",
            "source_semantics": (
                "exact_cat2_action_with_causally_linked_typed_immediate_outcome"
            ),
        },
    }


def _golden_rows() -> list[dict[str, object]]:
    # These states preserve the branch-relevant values of the real 12-row
    # session, including the one 0.527-second WW cutoff-sensitive sample.
    cases = [
        (_state(16, bloodrage=0, bloodthirst=0, whirlwind=0), _actual(off_gcd=["warrior_bloodrage"])),
        (_state(26, bloodrage=59.233, bloodthirst=0, whirlwind=0), _actual(gcd=["warrior_whirlwind"])),
        (_state(44, bloodrage=50.359, bloodthirst=1.909, whirlwind=0), _actual(gcd=["warrior_whirlwind"])),
        (_state(59, bloodrage=43.182, bloodthirst=1.09, whirlwind=1.582), _actual(queue=["warrior_heroic_strike"])),
        (_state(59, bloodrage=42.127, bloodthirst=0.035, whirlwind=0.527), _actual(queue=["warrior_heroic_strike"])),
        (_state(44, bloodrage=36.016, bloodthirst=0.394, whirlwind=0), _actual(gcd=["warrior_whirlwind"])),
        (_state(35, bloodrage=27.191, bloodthirst=1.008, whirlwind=0), _actual(gcd=["warrior_whirlwind"])),
        (_state(52, bloodrage=0, bloodthirst=5.827, whirlwind=1.054), _actual(queue=["warrior_heroic_strike"])),
        (_state(40, bloodrage=0, bloodthirst=4.554, whirlwind=0), _actual(gcd=["warrior_whirlwind"])),
        (_state(15, bloodrage=0, bloodthirst=4.107, whirlwind=8.3), _actual(off_gcd=["warrior_bloodrage"])),
        (_state(46, bloodrage=51.893, bloodthirst=2.2, whirlwind=0), _actual(gcd=["warrior_whirlwind"])),
        (_state(29, bloodrage=40.617, bloodthirst=3.786, whirlwind=0), _actual(gcd=["warrior_whirlwind"])),
    ]
    return [_row(index, state, actual) for index, (state, actual) in enumerate(cases, 1)]


def _write_transition_bundle(
    root: Path,
    rows: list[dict[str, object]],
) -> tuple[Path, Path]:
    dataset = root / "transitions.jsonl"
    manifest = root / "transitions.manifest.json"
    text = "".join(
        json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
        for row in rows
    )
    dataset.write_text(text, encoding="utf-8", newline="\n")
    identities = [
        {
            "export_session_id": row["identity"]["export_session_id"],
            "decision_id": row["identity"]["decision_id"],
        }
        for row in rows
    ]
    document = {
        "schema": "fury_shadow_transition_dataset_manifest/v1",
        "kind": "fury_shadow_transition_dataset",
        "record_schema": "fury_shadow_transition_fragment/v1",
        "output": str(dataset.resolve()),
        "output_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "row_count": len(rows),
        "export_session_id": SESSION,
        "identities": identities,
        "eligibility_contract": {
            "behavior_label_eligible": True,
            "offline_rl_episode_eligible": False,
            "deployment_allowed": False,
        },
        "commit": {
            "state": "complete",
            "manifest_written_last": True,
            "content_addressed": True,
        },
    }
    manifest.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return dataset, manifest


def _profile_snapshot(cat2_bytes: bytes) -> dict[str, object]:
    steps: list[dict[str, object]] = []
    definitions = [
        ("warrior_o2o_policy_brain", {"liveMode": False}),
        ("common_auto_attack", {}),
        ("warrior_berserker_stance", {}),
        ("warrior_bloodrage", {"maximumRage": 30}),
        ("warrior_execute", {}),
        ("warrior_bloodthirst", {}),
        ("warrior_whirlwind", {}),
        ("warrior_heroic_strike_alt", {"rageThreshold": 50}),
    ]
    for position, (card_id, options) in enumerate(definitions, start=1):
        steps.append(
            {
                "position": position,
                "id": card_id,
                "enabled": 1,
                "option_values": options,
            }
        )
    return {
        "artifact_type": "cat2_saved_profile_v1",
        "status": "ok",
        "authority_state": "CURRENT_UNSEALED_SOURCE_PROFILE",
        "execution_authorized": False,
        "deployment_allowed": False,
        "savedvariables": {
            "size_bytes": len(cat2_bytes),
            "mtime_ns": 123,
            "sha256": hashlib.sha256(cat2_bytes).hexdigest(),
        },
        "selection": {
            "active_profile_id": 1,
            "profile_order": [1],
        },
        "profile": {
            "id": 1,
            "name": "BrainOfCat Shadow",
            "steps": steps,
        },
        "raw_savedvariables_sha256": hashlib.sha256(cat2_bytes).hexdigest(),
        "profile_semantic_sha256": "b" * 64,
        "source_bundle_sha256": "c" * 64,
        "source_bundle": {
            "scope": "DIRECT_RUNTIME_DEPENDENCY_PINNED",
            "pin_status": "PINNED_EXACT",
            "transitive_dependency_closure_claimed": False,
        },
    }


class FuryCat2ProfileConformanceV1Tests(unittest.TestCase):
    def _inputs(
        self,
        root: Path,
        rows: list[dict[str, object]] | None = None,
    ) -> tuple[Path, Path, Path, Path, Path, dict[str, object]]:
        selected_rows = _golden_rows() if rows is None else rows
        dataset, manifest = _write_transition_bundle(root, selected_rows)
        cat2 = root / "Cat2.lua"
        cat2_bytes = b"Cat2CharacterDB = {}\n"
        cat2.write_bytes(cat2_bytes)
        installed = root / "Cat2"
        brain = root / "BrainOfCat"
        installed.mkdir()
        brain.mkdir()
        return dataset, manifest, cat2, installed, brain, _profile_snapshot(cat2_bytes)

    def test_golden_twelve_rows_are_only_assumption_conditioned_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset, manifest, cat2, installed, brain, snapshot = self._inputs(root)
            report, row_records = build_fury_cat2_profile_conformance(
                dataset,
                manifest,
                cat2,
                installed,
                brain,
                profile_snapshot=snapshot,
                rows_output_path=root / "audit.rows.jsonl",
            )

            self.assertEqual(report["inputs"]["transition_manifest_commit"], "PASS")
            self.assertEqual(report["strict_conformance"]["status"], "NOT_EVALUABLE")
            self.assertEqual(report["strict_conformance"]["input_complete_rows"], 0)
            diagnostic = report["assumption_conditioned_replay"]
            self.assertEqual(diagnostic["status"], "PASS")
            self.assertEqual(diagnostic["evaluated_rows"], 12)
            self.assertEqual(diagnostic["exact_factorized_action_match_rows"], 12)
            self.assertEqual(
                diagnostic["actual_action_counts"],
                {"bloodrage": 2, "heroic_strike": 3, "whirlwind": 7},
            )
            self.assertEqual(
                diagnostic["whirlwind_ready_boundary_sensitive_rows"], 1
            )
            self.assertEqual(report["profile"]["capture_time_binding"], "UNSEALED_ID_NAME_ONLY")
            self.assertFalse(report["eligibility"]["independent_expert_vote_allowed"])
            self.assertFalse(report["eligibility"]["deployment_allowed"])
            self.assertEqual(len(row_records), 12)
            self.assertTrue(all(not row["strict"]["input_complete"] for row in row_records))
            self.assertTrue(all(not row["outcome_or_reward_used_for_replay"] for row in row_records))

    def test_materializer_commits_rows_first_and_report_last(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset, manifest, cat2, installed, brain, snapshot = self._inputs(root)
            report_path = root / "audit.json"
            rows_path = root / "audit.rows.jsonl"

            def loader(*_args: object) -> dict[str, object]:
                return snapshot

            receipt = materialize_fury_cat2_profile_conformance(
                dataset,
                manifest,
                cat2,
                installed,
                brain,
                report_path,
                rows_path,
                profile_loader=loader,
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))
            row_bytes = rows_path.read_bytes()

            self.assertEqual(receipt["assumption_conditioned_match_rows"], 12)
            self.assertEqual(receipt["strict_input_complete_rows"], 0)
            self.assertEqual(
                hashlib.sha256(row_bytes).hexdigest(),
                report["output"]["rows_sha256"],
            )
            self.assertEqual(report["commit"]["state"], "complete")
            self.assertTrue(report["commit"]["report_written_last"])
            self.assertNotIn("rows", report)

    def test_rejects_dataset_changed_after_manifest_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset, manifest, cat2, installed, brain, snapshot = self._inputs(root)
            dataset.write_bytes(dataset.read_bytes() + b"\n")
            with self.assertRaisesRegex(
                FuryCat2ProfileConformanceError, "SHA256"
            ):
                build_fury_cat2_profile_conformance(
                    dataset,
                    manifest,
                    cat2,
                    installed,
                    brain,
                    profile_snapshot=snapshot,
                )

    def test_rejects_uncommitted_transition_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset, manifest, cat2, installed, brain, snapshot = self._inputs(root)
            document = json.loads(manifest.read_text(encoding="utf-8"))
            document["commit"]["state"] = "writing"
            manifest.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(
                FuryCat2ProfileConformanceError, "complete content-addressed"
            ):
                build_fury_cat2_profile_conformance(
                    dataset,
                    manifest,
                    cat2,
                    installed,
                    brain,
                    profile_snapshot=snapshot,
                )

    def test_rejects_profile_semantic_drift_and_incomplete_source_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset, manifest, cat2, installed, brain, snapshot = self._inputs(root)
            changed = copy.deepcopy(snapshot)
            changed["profile"]["steps"][7]["option_values"]["rageThreshold"] = 60
            with self.assertRaisesRegex(
                FuryCat2ProfileConformanceError, "rageThreshold"
            ):
                build_fury_cat2_profile_conformance(
                    dataset,
                    manifest,
                    cat2,
                    installed,
                    brain,
                    profile_snapshot=changed,
                )

            incomplete = copy.deepcopy(snapshot)
            incomplete["source_bundle"]["scope"] = "PARTIAL"
            with self.assertRaisesRegex(
                FuryCat2ProfileConformanceError, "direct runtime dependency"
            ):
                build_fury_cat2_profile_conformance(
                    dataset,
                    manifest,
                    cat2,
                    installed,
                    brain,
                    profile_snapshot=incomplete,
                )

    def test_profile_match_does_not_turn_action_mismatch_into_runtime_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = _golden_rows()
            rows[1]["actual"]["factorized_action"] = _actual(
                queue=["warrior_heroic_strike"]
            )
            dataset, manifest, cat2, installed, brain, snapshot = self._inputs(
                root, rows
            )
            report, row_records = build_fury_cat2_profile_conformance(
                dataset,
                manifest,
                cat2,
                installed,
                brain,
                profile_snapshot=snapshot,
            )

            self.assertEqual(report["assumption_conditioned_replay"]["status"], "FAIL")
            self.assertEqual(
                report["assumption_conditioned_replay"][
                    "exact_factorized_action_match_rows"
                ],
                11,
            )
            self.assertFalse(
                row_records[1]["assumption_conditioned"][
                    "exact_factorized_action_match"
                ]
            )
            self.assertEqual(report["strict_conformance"]["status"], "NOT_EVALUABLE")
            self.assertFalse(report["eligibility"]["deployment_allowed"])


if __name__ == "__main__":
    unittest.main()
