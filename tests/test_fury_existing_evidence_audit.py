from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.fury_existing_evidence_audit import build_audit, main


PLAYER = "0x0000000000000001"
TARGET = "0xF130000000000001"


def _state(raw_rage: int, **extra: object) -> dict[str, object]:
    value: dict[str, object] = {
        "playerGUID": PLAYER,
        "targetGUID": TARGET,
        "rageRaw": raw_rage,
        "rageRawScale": 10,
        "maximumRageRaw": 1000,
    }
    value.update(extra)
    return value


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def _empty_data_root(root: Path) -> Path:
    data_root = root / "offline_data"
    (data_root / "calibration").mkdir(parents=True)
    (data_root / "calibration_summaries").mkdir(parents=True)
    (data_root / "derived" / "chronicle_fury_partial_trajectory" / "v1").mkdir(
        parents=True
    )
    return data_root


class FuryExistingEvidenceAuditTests(unittest.TestCase):
    def test_unbridled_wrath_mirrors_are_one_proc_and_delta_is_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_root = _empty_data_root(Path(temporary_directory))
            _write_jsonl(
                data_root / "calibration" / "phase.jsonl",
                [
                    {
                        "sequence": 1,
                        "time": 10.000,
                        "event": "SPELL_DAMAGE_EVENT_SELF",
                        "spellID": 23894,
                        "amount": 500,
                        "hitInfo": 0,
                        "sourceGUID": PLAYER,
                        "targetGUID": TARGET,
                        "state": _state(100),
                    },
                    {
                        "sequence": 2,
                        "time": 10.0005,
                        "event": "UNIT_RAGE",
                        "state": _state(100),
                    },
                    {
                        "sequence": 3,
                        "time": 10.001,
                        "event": "SPELL_ENERGIZE_BY_SELF",
                        "spellID": 12964,
                        "amount": 20,
                        "sourceGUID": PLAYER,
                        "targetGUID": PLAYER,
                        "state": _state(100),
                    },
                    {
                        "sequence": 4,
                        "time": 10.002,
                        "event": "SPELL_ENERGIZE_ON_SELF",
                        "spellID": 12964,
                        "amount": 20,
                        "sourceGUID": PLAYER,
                        "targetGUID": PLAYER,
                        "state": _state(100),
                    },
                    {
                        "sequence": 5,
                        "time": 10.050,
                        "event": "UNIT_RAGE",
                        "state": _state(120),
                    },
                ],
            )

            audit = build_audit(data_root)
            evidence = audit["mechanisms"]["unbridled_wrath"]["calibration"]

            self.assertEqual(evidence["raw_mirrored_event_count"], 2)
            self.assertEqual(evidence["unique_proc_count"], 1)
            self.assertEqual(evidence["mirrored_events_removed"], 1)
            self.assertEqual(evidence["trigger_attribution_counts"]["bloodthirst"], 1)
            self.assertEqual(evidence["actual_resource_delta"]["identifiable_count"], 1)
            self.assertEqual(
                evidence["actual_resource_delta"]["confirmed_advertised_delta_count"], 1
            )

    def test_energize_presence_without_resource_snapshot_stays_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_root = _empty_data_root(Path(temporary_directory))
            rows: list[dict[str, object]] = []
            for proc in range(3):
                base = 20.0 + proc
                rows.extend(
                    [
                        {
                            "sequence": proc * 2 + 1,
                            "time": base,
                            "event": "SPELL_ENERGIZE_BY_SELF",
                            "spellID": 12964,
                            "amount": 20,
                            "sourceGUID": PLAYER,
                            "targetGUID": PLAYER,
                            "state": _state(200),
                        },
                        {
                            "sequence": proc * 2 + 2,
                            "time": base + 0.001,
                            "event": "SPELL_ENERGIZE_ON_SELF",
                            "spellID": 12964,
                            "amount": 20,
                            "sourceGUID": PLAYER,
                            "targetGUID": PLAYER,
                            "state": _state(200),
                        },
                    ]
                )
            _write_jsonl(data_root / "calibration" / "phase.jsonl", rows)

            mechanism = build_audit(data_root)["mechanisms"]["unbridled_wrath"]

            self.assertEqual(mechanism["status"], "PARTIAL")
            self.assertEqual(mechanism["calibration"]["unique_proc_count"], 3)
            self.assertEqual(
                mechanism["calibration"]["actual_resource_delta"][
                    "confirmed_advertised_delta_count"
                ],
                0,
            )
            self.assertTrue(
                mechanism["calibration"]["actual_resource_delta"][
                    "presence_is_not_counted_as_applied_rage"
                ]
            )

    def test_battle_shout_complete_transition_is_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_root = _empty_data_root(Path(temporary_directory))
            before_ap = {"effective": 1000}
            after_ap = {"effective": 1290}
            _write_jsonl(
                data_root / "calibration" / "phase.jsonl",
                [
                    {
                        "sequence": 1,
                        "time": 30.0,
                        "event": "SPELL_GO_SELF",
                        "spellID": 25289,
                        "state": _state(1000, attackPower=before_ap, gcd=1.5),
                    },
                    {
                        "sequence": 2,
                        "time": 30.001,
                        "event": "AURA_CAST_ON_SELF",
                        "spellID": 25289,
                        "state": _state(1000, attackPower=before_ap, gcd=1.499),
                    },
                    {
                        "sequence": 3,
                        "time": 30.002,
                        "event": "BUFF_UPDATE_DURATION_SELF",
                        "arg2": 120000,
                        "state": _state(1000, attackPower=before_ap, gcd=1.498),
                    },
                    {
                        "sequence": 4,
                        "time": 30.003,
                        "event": "UNIT_RAGE",
                        "state": _state(900, attackPower=after_ap, gcd=1.497),
                    },
                ],
            )

            mechanism = build_audit(data_root)["mechanisms"]["battle_shout"]

            self.assertEqual(mechanism["status"], "VERIFIED")
            self.assertEqual(mechanism["complete_transition_count"], 1)
            observation = mechanism["observations"][0]
            self.assertEqual(observation["rage_cost"], 10)
            self.assertEqual(observation["attack_power_delta"], 290)
            self.assertEqual(observation["duration_ms"], 120000)

    def test_cli_writes_only_requested_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            data_root = _empty_data_root(root)
            output = root / "result" / "audit.json"

            return_code = main(
                ["--data-root", str(data_root), "--output", str(output)]
            )

            self.assertEqual(return_code, 0)
            self.assertTrue(output.is_file())
            document = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(document["kind"], "fury_existing_evidence_audit")
            self.assertFalse(document["simulator_modified"])
            self.assertFalse(document["registry_modified"])


if __name__ == "__main__":
    unittest.main()
