from __future__ import annotations

import gzip
import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.chronicle_fury_behavior_prior import (
    ACTION_TO_CAT2_CARD,
    BehaviorPriorError,
    EmpiricalBackoffPrior,
    Example,
    action_mapping_contract,
    build_documents,
    cross_validate,
    load_examples,
)


LABELS = ("warrior.slam", "warrior.whirlwind")


def _record(
    *,
    source: str,
    player: str,
    encounter: str,
    sequence: int,
    label: str,
    history_spell_ids: list[int],
    swing_observed: bool,
) -> dict[str, object]:
    start_index = sequence * 10 + 10
    history = [
        {
            "decision_id": f"prior-{sequence}-{index}",
            "spell_id": spell_id,
            "spell_name": "Prior",
            "result": "succeeded",
            "result_event_index": start_index - len(history_spell_ids) + index - 1,
        }
        for index, spell_id in enumerate(history_spell_ids)
    ]
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
            "normalized_file": f"{source}.jsonl",
            "start_anchor": {
                "kind": "OBSERVED",
                "event_index": start_index,
                "csv_line": start_index + 100,
                "offset_ms": start_index * 10,
                "note": "server-observed START candidate",
            },
        },
        "action": {
            "decision_id": f"{source}:{player}:{sequence}",
            "semantics": "server_observed_START_candidate",
            "spell_id": 45961 if label == "warrior.slam" else 1680,
            "spell_name": "Slam" if label == "warrior.slam" else "Whirlwind",
            "policy_action_key": label,
            "catalog_status": "MAPPED_ACTIVE",
            "lane": "gcd",
        },
        "result": {
            "association": "unique",
            "status": "succeeded",
            "anchor": {"event_index": start_index + 1},
            "start_to_result_ms": 10,
            "candidate_action_ids": [f"{source}:{player}:{sequence}"],
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
            "recent_uniquely_linked_server_actions": history,
            "last_auto_attack_elapsed_ms": 1500 if swing_observed else None,
        },
        "state_mask": {
            "recent_uniquely_linked_server_actions": True,
            "last_auto_attack_elapsed_ms": swing_observed,
        },
        "state_provenance": {},
        "window_until_next_start_candidate": {
            "outgoing_damage_observed": 9999,
            "rage_gain_chronicle_units": 500,
            "auto_attack_rows": 1,
            "next_start_anchor": None,
            "causal_reward": None,
            "causal_attribution": "MISSING",
        },
    }


def _write_dataset(root: Path) -> Path:
    partitions = []
    eligible = 0
    for group in range(5):
        source = f"source-{group}"
        player = f"player-{group}"
        partition = root / f"{source}.jsonl.gz"
        records = []
        previous: list[int] = []
        for sequence in range(8):
            if previous and previous[-1] == 45961:
                label = "warrior.whirlwind"
                spell_id = 1680
            else:
                label = "warrior.slam"
                spell_id = 45961
            records.append(
                _record(
                    source=source,
                    player=player,
                    encounter=f"encounter-{group}",
                    sequence=sequence,
                    label=label,
                    history_spell_ids=previous[-3:],
                    swing_observed=sequence % 2 == 0,
                )
            )
            previous.append(spell_id)
        with gzip.open(partition, "wt", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        eligible += len(records)
        partitions.append({"partition": str(partition), "decision_count": len(records)})
    manifest = root / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "schema": "chronicle_fury_decision_dataset/v1",
                "kind": "chronicle_fury_decision_dataset_manifest",
                "output": {"observable_behavior_labels": eligible},
                "quality": {
                    "offline_rl_ready": False,
                    "queue_intent_labels": 0,
                    "causal_reward_rows": 0,
                },
                "partitions": partitions,
            }
        ),
        encoding="utf-8",
    )
    return manifest


class ChronicleFuryBehaviorPriorTests(unittest.TestCase):
    def test_group_cv_and_documents_preserve_analysis_only_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest = _write_dataset(Path(temporary_directory))
            examples, input_info = load_examples(manifest)
            evaluation = cross_validate(examples)

            self.assertEqual(len(examples), 40)
            self.assertEqual(len(evaluation["folds"]), 5)
            self.assertTrue(
                all(not fold["source_overlap"] for fold in evaluation["folds"])
            )
            self.assertTrue(
                all(not fold["player_overlap"] for fold in evaluation["folds"])
            )
            for name in ("global", "last_action", "markov_2"):
                metrics = evaluation["aggregate_held_out"][name]
                self.assertEqual(metrics["examples"], 40)
                self.assertIn("top1_accuracy", metrics)
                self.assertIn("top3_accuracy", metrics)
                self.assertIn("log_loss", metrics)
                self.assertIn("macro_recall", metrics)
                self.assertEqual(sum(v["support"] for v in metrics["per_class"].values()), 40)

            available = {ACTION_TO_CAT2_CARD[label] for label in LABELS}
            model, report, card = build_documents(
                examples,
                input_info=input_info,
                available_card_ids=available,
                cat2_card_source=Path(temporary_directory) / "deployed-cat2-cards",
            )
            self.assertTrue(model["analysis_only"])
            self.assertFalse(model["action_mapping"]["deployment_allowed"])
            self.assertTrue(model["action_mapping"]["mapping_complete"])
            self.assertEqual(
                model["action_mapping"]["cat2_card_source"],
                str((Path(temporary_directory) / "deployed-cat2-cards").resolve()),
            )
            self.assertIn("queue intent", model["training_contract"]["never_used"])
            self.assertEqual(
                report["interpretation"]["promotion_status"],
                "ANALYSIS_BASELINE_ONLY",
            )
            self.assertIn("not full-state behavior", card)

    def test_markov_context_uses_only_two_prior_actions_and_explicit_mask_bucket(self) -> None:
        examples = [
            Example(
                "source",
                "player",
                "encounter",
                f"decision-{index}",
                "warrior.slam",
                ("old", "middle", "new"),
                "MISSING",
            )
            for index in range(3)
        ]
        model = EmpiricalBackoffPrior(LABELS, max_order=2)
        model.fit(examples)
        self.assertIn(("MISSING", "middle", "new"), model.context_counts[2])
        self.assertNotIn(("MISSING", "old", "middle", "new"), model.context_counts[2])
        _, selected_order = model.probabilities(examples[0])
        self.assertEqual(selected_order, 2)

    def test_rejects_future_history_and_invented_causal_reward(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manifest = _write_dataset(root)
            first_partition = Path(
                json.loads(manifest.read_text(encoding="utf-8"))["partitions"][0][
                    "partition"
                ]
            )
            with gzip.open(first_partition, "rt", encoding="utf-8") as handle:
                records = [json.loads(line) for line in handle]
            records[1]["state_before"]["recent_uniquely_linked_server_actions"][0][
                "result_event_index"
            ] = records[1]["source"]["start_anchor"]["event_index"]
            with gzip.open(first_partition, "wt", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record) + "\n")
            with self.assertRaisesRegex(BehaviorPriorError, "strictly pre-decision"):
                load_examples(manifest)

            manifest = _write_dataset(root)
            with gzip.open(first_partition, "rt", encoding="utf-8") as handle:
                records = [json.loads(line) for line in handle]
            records[0]["window_until_next_start_candidate"]["causal_reward"] = 10
            with gzip.open(first_partition, "wt", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record) + "\n")
            with self.assertRaisesRegex(BehaviorPriorError, "causal reward"):
                load_examples(manifest)

    def test_unmapped_or_absent_cat2_card_blocks_deployment_mapping(self) -> None:
        contract = action_mapping_contract(
            ["warrior.slam", "warrior.unknown"], {"warrior_whirlwind"}
        )
        self.assertFalse(contract["mapping_complete"])
        self.assertFalse(contract["deployment_allowed"])
        self.assertEqual(contract["unmapped_action_keys"], ["warrior.unknown"])
        self.assertEqual(contract["missing_cat2_card_ids"], ["warrior_slam"])


if __name__ == "__main__":
    unittest.main()
