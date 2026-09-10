from __future__ import annotations

import gzip
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps.chronicle_fury_l2_context_prior import (
    CONSUMED_L2_FIELDS,
    L2ContextPriorError,
    _joined_example_from_records,
    cross_validate,
    load_joined_examples,
    train_l2_context_prior,
)


def _decision(
    *,
    source: str,
    player: str,
    encounter: str,
    sequence: int,
    label: str,
    history: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    event_index = sequence * 10 + 10
    spell_id = 45961 if label == "warrior.slam" else 1680
    return {
        "schema_version": 1,
        "schema": "chronicle_fury_decision/v1",
        "identity": {
            "source_instance_ref": source,
            "encounter_id": encounter,
            "player_guid": player,
            "player_name": player,
            "board_spec": "Fury",
            "leaderboard_rows": [],
            "identity_status": "VERIFIED",
        },
        "source": {
            "start_anchor": {
                "kind": "OBSERVED",
                "event_index": event_index,
                "csv_line": event_index + 2,
                "offset_ms": sequence * 1_000,
            }
        },
        "action": {
            "decision_id": f"{encounter}:{player}:{sequence}",
            "semantics": "server_observed_START_candidate",
            "spell_id": spell_id,
            "spell_name": "Slam" if spell_id == 45961 else "Whirlwind",
            "policy_action_key": label,
            "catalog_status": "MAPPED_ACTIVE",
            "lane": "gcd",
        },
        "result": {
            "association": "unique",
            "status": "succeeded",
            "anchor": {"event_index": event_index + 1},
            "start_to_result_ms": 10,
            "candidate_action_ids": [f"{encounter}:{player}:{sequence}"],
        },
        "eligibility": {
            "observable_behavior_label": True,
            "partial_observation_bc": True,
            "full_state_bc": False,
            "offline_rl": False,
            "queue_intent_supervision": False,
            "causal_reward_supervision": False,
            "readiness_full_state_ready": False,
            "full_state_blockers": ["absolute_rage_anchor"],
        },
        "state_before": {
            "recent_uniquely_linked_server_actions": history or [],
            "last_auto_attack_elapsed_ms": 1_500,
        },
        "state_mask": {
            "recent_uniquely_linked_server_actions": True,
            "last_auto_attack_elapsed_ms": True,
        },
        "state_provenance": {},
        "window_until_next_start_candidate": {
            "outgoing_damage_observed": 0,
            "rage_gain_chronicle_units": 0,
            "auto_attack_rows": 0,
            "next_start_anchor": None,
            "causal_reward": None,
            "causal_attribution": "MISSING",
        },
    }


def _field(value: object, mask: str = "RECONSTRUCTED") -> dict[str, object]:
    return {
        "value": value,
        "mask": mask,
        "provenance": {"kind": mask},
    }


def _l2(
    decision: dict[str, object],
    *,
    target_count: int | None,
    positive_pile: bool,
    wave_elapsed_ms: int | None,
) -> dict[str, object]:
    identity = decision["identity"]
    action = decision["action"]
    anchor = decision["source"]["start_anchor"]
    target_field = (
        _field(None, "MISSING")
        if target_count is None
        else _field(target_count)
    )
    elapsed_field = (
        _field(None, "MISSING")
        if wave_elapsed_ms is None
        else _field(wave_elapsed_ms)
    )
    pile_field = (
        _field(
            {
                "components": [["target-a", "target-b"]],
                "pair_evidence_counts": [],
                "semantics": "positive evidence only",
            }
        )
        if positive_pile
        else _field(None, "MISSING")
    )
    return {
        "schema_version": 1,
        "schema": "chronicle_fury_l2_temporal_context/v1",
        "decision": {
            "decision_id": action["decision_id"],
            "source_instance_ref": identity["source_instance_ref"],
            "encounter_id": identity["encounter_id"],
            "player_guid": identity["player_guid"],
            "action_spell_id": action["spell_id"],
            "action_spell_name": action["spell_name"],
            "start_anchor": {
                "kind": "OBSERVED",
                "event_index": anchor["event_index"],
                "csv_line": anchor["csv_line"],
                "offset_ms": anchor["offset_ms"],
            },
        },
        "join": {
            "key": {
                "instance": identity["source_instance_ref"],
                "encounter": identity["encounter_id"],
                "offset_ms": anchor["offset_ms"],
                "event_index": anchor["event_index"],
            },
            "status": "MATCHED",
            "future_leakage_rule": "prefix only",
        },
        "fields": {
            "target_count": target_field,
            "pile_cohit_components": pile_field,
            "wave_elapsed_ms": elapsed_field,
            "recent_action_history": _field(
                decision["state_before"]["recent_uniquely_linked_server_actions"]
            ),
            # Values below are deliberately present to prove the consumer does
            # not read them.
            "current_wave_final_duration_ms": _field(999_999),
            "previous_wave_duration_ms": _field(888_888),
            "alive_target_proxy": _field({"count": 77}),
            "target_hp": _field(123_456),
        },
    }


def _write_fixture(root: Path) -> Path:
    entries = []
    total = 0
    for group in range(5):
        source = f"instance-{group}"
        player = f"player-{group}"
        decision_path = root / f"{source}.decisions.jsonl.gz"
        l2_path = root / f"{source}.l2.jsonl.gz"
        decisions = []
        sidecars = []
        for sequence in range(8):
            multi = sequence % 2 == 1
            label = "warrior.whirlwind" if multi else "warrior.slam"
            decision = _decision(
                source=source,
                player=player,
                encounter=f"encounter-{group}",
                sequence=sequence,
                label=label,
            )
            decisions.append(decision)
            sidecars.append(
                _l2(
                    decision,
                    target_count=3 if multi else 1,
                    positive_pile=multi,
                    wave_elapsed_ms=20_000 if multi else 1_000,
                )
            )
        with gzip.open(decision_path, "wt", encoding="utf-8") as handle:
            for value in decisions:
                handle.write(json.dumps(value, separators=(",", ":")) + "\n")
        with gzip.open(l2_path, "wt", encoding="utf-8") as handle:
            for value in sidecars:
                handle.write(json.dumps(value, separators=(",", ":")) + "\n")
        entries.append(
            {
                "decision_partition": str(decision_path),
                "output_partition": str(l2_path),
                "record_count": len(decisions),
            }
        )
        total += len(decisions)
    manifest = root / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "chronicle_fury_l2_temporal_join_manifest_v1",
                "inputs": {
                    "decision_manifest": str(root / "decision-manifest.json"),
                    "normalized_or_raw_rows_read": 0,
                },
                "summary": {"record_count": total},
                "partitions": entries,
            }
        ),
        encoding="utf-8",
    )
    return manifest


class ChronicleFuryL2ContextPriorTests(unittest.TestCase):
    def test_exact_join_instance_disjoint_cv_and_context_gain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest = _write_fixture(Path(temporary_directory))
            examples, info = load_joined_examples(manifest)
            evaluation = cross_validate(examples)

            self.assertEqual(len(examples), 40)
            self.assertEqual(info["exact_three_key_matches"], 40)
            self.assertEqual(info["normalized_or_raw_rows_read"], 0)
            self.assertTrue(
                all(not fold["instance_overlap"] for fold in evaluation["folds"])
            )
            metrics = evaluation["aggregate_held_out"]
            self.assertGreater(
                metrics["l2_context_successful_suffix_2"]["top1_accuracy"],
                metrics["successful_suffix_2"]["top1_accuracy"],
            )
            self.assertGreater(
                evaluation["paired_deltas"]["l2_minus_legacy_markov_2_replay"][
                    "top1_accuracy"
                ],
                0,
            )

    def test_exact_three_key_mismatch_is_rejected(self) -> None:
        decision = _decision(
            source="instance-a",
            player="player-a",
            encounter="encounter-a",
            sequence=0,
            label="warrior.slam",
        )
        row = _l2(
            decision,
            target_count=1,
            positive_pile=False,
            wave_elapsed_ms=100,
        )
        row["decision"]["decision_id"] = "wrong-decision"
        row["join"]["key"]["instance"] = "instance-a"
        with self.assertRaisesRegex(L2ContextPriorError, "exact L2 join mismatch"):
            _joined_example_from_records(decision, row)

    def test_only_successful_suffix_is_used_and_missing_is_explicit(self) -> None:
        history = [
            {
                "spell_id": 45961,
                "spell_name": "Slam",
                "result": "succeeded",
                "result_event_index": 1,
            },
            {
                "spell_id": 1680,
                "spell_name": "Whirlwind",
                "result": "failed",
                "result_event_index": 2,
            },
            {
                "spell_id": 12328,
                "spell_name": "Death Wish",
                "result": "succeeded",
                "result_event_index": 3,
            },
        ]
        decision = _decision(
            source="instance-a",
            player="player-a",
            encounter="encounter-a",
            sequence=1,
            label="warrior.slam",
            history=history,
        )
        row = _l2(
            decision,
            target_count=None,
            positive_pile=False,
            wave_elapsed_ms=None,
        )
        example = _joined_example_from_records(decision, row)
        self.assertEqual(example.successful_action_suffix, ("id:45961", "id:12328"))
        self.assertEqual(example.target_count_bucket, "MISSING")
        self.assertEqual(example.wave_elapsed_bucket, "MISSING")
        self.assertEqual(example.pile_evidence, "NO_POSITIVE_COHIT_EVIDENCE")

    def test_forbidden_final_fields_cannot_change_features(self) -> None:
        decision = _decision(
            source="instance-a",
            player="player-a",
            encounter="encounter-a",
            sequence=1,
            label="warrior.slam",
        )
        first = _l2(
            decision,
            target_count=2,
            positive_pile=True,
            wave_elapsed_ms=7_000,
        )
        second = json.loads(json.dumps(first))
        second["fields"]["current_wave_final_duration_ms"]["value"] = 1
        second["fields"]["previous_wave_duration_ms"]["value"] = 2
        second["fields"]["alive_target_proxy"]["value"] = {"count": 1}
        second["fields"]["target_hp"]["value"] = 3
        left = _joined_example_from_records(decision, first)
        right = _joined_example_from_records(decision, second)
        self.assertEqual(left, right)
        self.assertEqual(
            set(CONSUMED_L2_FIELDS),
            {
                "target_count",
                "pile_cohit_components",
                "wave_elapsed_ms",
                "recent_action_history",
            },
        )

    def test_training_writes_only_compact_analysis_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manifest = _write_fixture(root)
            output = root / "model"
            result = train_l2_context_prior(
                manifest,
                output_dir=output,
                reference_evaluation=None,
            )
            model = json.loads(result.model_path.read_text(encoding="utf-8"))
            report = json.loads(result.report_path.read_text(encoding="utf-8"))
            self.assertTrue(model["analysis_only"])
            self.assertFalse(model["deployment_allowed"])
            self.assertEqual(
                report["interpretation"]["promotion_status"],
                "ANALYSIS_ONLY_NOT_DEPLOYABLE",
            )
            self.assertNotIn("decision_id", result.model_path.read_text(encoding="utf-8"))
            self.assertLess(result.model_path.stat().st_size, 100_000)
            self.assertTrue(result.model_card_path.is_file())


if __name__ == "__main__":
    unittest.main()
