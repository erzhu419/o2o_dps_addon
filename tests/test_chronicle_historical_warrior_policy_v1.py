from __future__ import annotations

from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.chronicle_historical_warrior_policy_v1 import (
    ARMS_LANE,
    FURY_LANE,
    HistoricalWarriorPolicyError,
    IMPLEMENTATION_REVISION as POLICY_IMPLEMENTATION_REVISION,
    build_chronicle_historical_warrior_policy,
    predict_action_distribution,
    sample_legal_action,
    validate_historical_warrior_policy_evaluation,
    validate_historical_warrior_policy_model,
)
from o2o_dps.chronicle_team_wave_model_v1 import (
    IMPLEMENTATION_REVISION as TEAM_MODEL_IMPLEMENTATION_REVISION,
    SCHEMA as TEAM_MODEL_SCHEMA,
)


TARGET = "0xF130000000000001"
DEAD_TARGET = "0xF1300000000000DD"


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256_json(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_gzip(path: Path, rows: list[dict[str, object]]) -> str:
    logical = b"".join(_canonical_bytes(row) + b"\n" for row in rows)
    with path.open("wb") as raw:
        with gzip.GzipFile(
            filename="", fileobj=raw, mode="wb", compresslevel=9, mtime=0
        ) as compressed:
            compressed.write(logical)
    return hashlib.sha256(logical).hexdigest()


def _state(
    order_key: list[int],
    wave_offset_ms: int,
    recent: list[dict[str, object]],
    *,
    future_key: bool = False,
) -> dict[str, object]:
    value: dict[str, object] = {
        "cutoff_exclusive_order_key": order_key,
        "cutoff_semantics": "strictly before current event order_key",
        "prefix_event_count": len(recent),
        "wave_elapsed_ms": wave_offset_ms,
        "seen_target_guids": [TARGET, DEAD_TARGET],
        "alive_seen_target_guids": [TARGET],
        "observed_dead_target_guids": [DEAD_TARGET],
        "actor_last_observed_target_guid": TARGET if recent else None,
        "actor_recent_observed_events": deepcopy(recent),
        "leave_one_player_out_background_before": {
            "included_background_damage": 400 + wave_offset_ms,
            "prefix_elapsed_average_background_dps": 900,
            "included_unattributed_damage": 0,
        },
        "observed_target_prefix": {
            TARGET: {"seen": True, "dead": False},
            DEAD_TARGET: {"seen": True, "dead": True},
        },
    }
    if future_key:
        value["future_teammate_actions"] = [{"spell": "Leak"}]
    return value


def _transition(
    event_type: str,
    spell_id: int,
    spell_name: str,
    *,
    offset: int,
    serial: int,
    recent: list[dict[str, object]],
    future_key: bool = False,
    bad_hash: bool = False,
) -> dict[str, object]:
    spell = {"id": spell_id, "name": spell_name}
    target = {"guid": TARGET, "name": "Target"}
    order = [offset, serial, 1000 + serial]
    observed = {
        "order_key": order,
        "wave_offset_ms": offset,
        "event_type": event_type,
        "spell": spell,
        "target": target,
    }
    core: dict[str, object] = {
        "state_before": _state(
            order, offset, recent, future_key=future_key
        ),
        "observed_event": observed,
        "action_observation": {
            "event_type": event_type,
            "spell": spell,
            "target": target,
        },
        "feature_cutoff_is_strict_prefix": True,
        "future_outcomes_in_state_before": False,
    }
    return {
        **core,
        "transition_sha256": "0" * 64 if bad_hash else _sha256_json(core),
    }


def _episode(
    component_index: int,
    lane: str,
    contamination_status: str,
    membership: dict[str, object],
    *,
    future_key: bool = False,
    bad_hash: bool = False,
) -> dict[str, object]:
    if lane == FURY_LANE:
        primary = (23881, "Bloodthirst")
        secondary = (1680, "Whirlwind")
        name = "托尼牛" if component_index == 0 else f"Fury-{component_index}"
    else:
        primary = (12294, "Mortal Strike")
        secondary = (7384, "Overpower")
        name = "桃姬儿" if component_index == 2 else f"Arms-{component_index}"
    start_recent: list[dict[str, object]] = []
    start = _transition(
        "START",
        primary[0],
        primary[1],
        offset=100,
        serial=1,
        recent=start_recent,
        future_key=future_key,
        bad_hash=bad_hash,
    )
    prior_start = [
        {
            "event_type": "START",
            "spell": {"id": primary[0], "name": primary[1]},
            "target": {"guid": TARGET},
        }
    ]
    paired_cast = _transition(
        "CAST",
        primary[0],
        primary[1],
        offset=110,
        serial=2,
        recent=prior_start,
    )
    prior_cast = prior_start + [
        {
            "event_type": "CAST",
            "spell": {"id": primary[0], "name": primary[1]},
            "target": {"guid": TARGET},
        }
    ]
    instant = _transition(
        "CAST",
        secondary[0],
        secondary[1],
        offset=2500 + component_index * 5,
        serial=3,
        recent=prior_cast,
    )
    eligible_contamination = contamination_status in {
        "POSTFIX_KNOWN_CLEAN",
        "NO_KNOWN_RULE_MATCH",
    }
    return {
        "schema": TEAM_MODEL_SCHEMA,
        "record_type": "player_wave_episode",
        "episode_id": _sha256_json(
            {"component": component_index, "lane": lane}
        ),
        "player": {
            "guid": f"player-{lane.casefold()}-{component_index}",
            "name": name,
            "hero_class": "WARRIOR",
            "specialization": {
                "partition_key": lane,
                "status": "OBSERVED_SINGLE",
            },
        },
        "component_membership": deepcopy(membership),
        "eligibility": {
            "contamination_status": contamination_status,
            "historical_fury_policy_training_eligible": (
                eligible_contamination and lane == FURY_LANE
            ),
            "team_behavior_training_eligible": eligible_contamination,
        },
        "prefix_transitions": [start, paired_cast, instant],
        # These descriptive outcomes are intentionally present.  The policy
        # compiler must neither read nor copy them into decision features.
        "leave_one_player_out_background": {
            "final_team_damage": 999999,
            "wave_duration_ms": 777777,
        },
        "exact_trace_lane": {
            "status": "DESCRIPTIVE_NONVOTING",
            "voting_eligible": False,
        },
    }


def _fixture(
    root: Path,
    *,
    future_key: bool = False,
    bad_transition_hash: bool = False,
) -> tuple[Path, Path]:
    source = root / "source"
    source.mkdir(parents=True)
    statuses = [
        "SUSPECT_36YD_RANGE_BUG",
        "UNKNOWN_NONVOTING",
        "POSTFIX_KNOWN_CLEAN",
        "POSTFIX_KNOWN_CLEAN",
        *(["NO_KNOWN_RULE_MATCH"] * 8),
    ]
    rows: list[dict[str, object]] = []
    connected_components: list[dict[str, object]] = []
    node_to_component: list[dict[str, object]] = []
    nodes: list[dict[str, object]] = []
    for component_index, contamination in enumerate(statuses):
        component_id = _sha256_json({"component": component_index})
        instance_node = f"instance-node-{component_index}"
        guild_node = f"guild-node-{component_index}"
        fury_node = f"player-fury-node-{component_index}"
        arms_node = f"player-arms-node-{component_index}"
        members = [instance_node, guild_node, fury_node, arms_node]
        connected_components.append(
            {"component_id": component_id, "node_ids": members}
        )
        for node_id, kind in (
            (instance_node, "INSTANCE"),
            (guild_node, "GUILD"),
            (fury_node, "PLAYER"),
            (arms_node, "PLAYER"),
        ):
            nodes.append({"node_id": node_id, "kind": kind})
            node_to_component.append(
                {"node_id": node_id, "component_id": component_id}
            )
        for lane, player_node in ((FURY_LANE, fury_node), (ARMS_LANE, arms_node)):
            membership = {
                "instance_node_id": instance_node,
                "player_node_id": player_node,
                "guild_node_ids": [guild_node],
                "required_split_unit": "connected component",
                "row_random_split_allowed": False,
            }
            rows.append(
                _episode(
                    component_index,
                    lane,
                    contamination,
                    membership,
                    future_key=future_key and component_index == 2 and lane == FURY_LANE,
                    bad_hash=(
                        bad_transition_hash
                        and component_index == 2
                        and lane == FURY_LANE
                    ),
                )
            )
    partition = source / "fixture.jsonl.gz"
    logical_sha = _write_gzip(partition, rows)
    core: dict[str, object] = {
        "schema": TEAM_MODEL_SCHEMA,
        "schema_version": 1,
        "kind": "chronicle_team_wave_model_manifest",
        "implementation_revision": TEAM_MODEL_IMPLEMENTATION_REVISION,
        "claim_boundary": {
            "exact_trace_status": "DESCRIPTIVE_NONVOTING",
            "exact_trace_can_vote": False,
            "learned_generator_present": False,
            "comparison_ready": False,
            "fourth_voting_baseline_present": False,
            "arms_common_action_diagnostic_only": True,
            "multiseed_12_cells_ready": False,
        },
        "split_graph": {
            "required_split_unit": "connected component",
            "row_random_split_allowed": False,
            "same_player_or_guild_can_cross_folds": False,
            "nodes": nodes,
            "edges": [],
            "connected_components": connected_components,
            "node_to_component": node_to_component,
        },
        "partitions": [
            {
                "instance_id": "synthetic-multi-instance",
                "partition": partition.name,
                "logical_content_sha256": logical_sha,
                "compressed_file_sha256": _sha256_file(partition),
                "compressed_size_bytes": partition.stat().st_size,
                "record_count": len(rows),
                "player_wave_episode_count": len(rows),
                "voting_ready": False,
                "exact_trace_status": "DESCRIPTIVE_NONVOTING",
            }
        ],
    }
    manifest = {
        **core,
        "content_address": {
            "algorithm": "sha256",
            "scope": "canonical JSON document excluding content_address",
            "sha256": _sha256_json(core),
        },
    }
    manifest_path = source / "team_manifest.json"
    manifest_path.write_bytes(_canonical_bytes(manifest) + b"\n")
    return manifest_path, partition


def _load(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _runtime_state(*, future_key: bool = False) -> dict[str, object]:
    return _state(
        [9000, 50, 1050],
        9000,
        [
            {
                "event_type": "CAST",
                "spell": {"id": 23881, "name": "Bloodthirst"},
                "target": {"guid": TARGET},
            }
        ],
        future_key=future_key,
    )


class ChronicleHistoricalWarriorPolicyV1Tests(unittest.TestCase):
    def test_stale_model_and_policy_revisions_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            source_manifest, _ = _fixture(root)
            stale_source = _load(source_manifest)
            stale_source["implementation_revision"] = "stale-cutoff-revision"
            source_core = {
                key: value
                for key, value in stale_source.items()
                if key != "content_address"
            }
            stale_source["content_address"]["sha256"] = _sha256_json(source_core)
            source_manifest.write_bytes(_canonical_bytes(stale_source) + b"\n")
            with self.assertRaisesRegex(
                HistoricalWarriorPolicyError, "implementation revision"
            ):
                build_chronicle_historical_warrior_policy(
                    team_wave_model_manifest_path=source_manifest,
                    output_directory=root / "stale-source",
                )

            current_source, _ = _fixture(root / "current")
            result = build_chronicle_historical_warrior_policy(
                team_wave_model_manifest_path=current_source,
                output_directory=root / "current-policy",
            )
            policy = _load(result.model)
            self.assertEqual(
                POLICY_IMPLEMENTATION_REVISION,
                policy["implementation_revision"],
            )
            policy["implementation_revision"] = "stale-policy-revision"
            policy_core = {
                key: value
                for key, value in policy.items()
                if key != "content_address"
            }
            policy["content_address"]["sha256"] = _sha256_json(policy_core)
            with self.assertRaisesRegex(
                HistoricalWarriorPolicyError, "implementation revision"
            ):
                validate_historical_warrior_policy_model(policy)

    def test_build_is_deterministic_content_addressed_and_nonvoting(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            manifest, _ = _fixture(root)
            first = build_chronicle_historical_warrior_policy(
                team_wave_model_manifest_path=manifest,
                output_directory=root / "first",
            )
            second = build_chronicle_historical_warrior_policy(
                team_wave_model_manifest_path=manifest,
                output_directory=root / "second",
            )
            first_model = _load(first.model)
            second_model = _load(second.model)
            self.assertEqual(first_model, second_model)
            first_evaluation = _load(first.evaluation)
            second_evaluation = _load(second.evaluation)
            self.assertEqual(first_evaluation, second_evaluation)
            validate_historical_warrior_policy_model(first_model)
            validate_historical_warrior_policy_evaluation(
                first_evaluation,
                model_content_sha256=first_model["content_address"]["sha256"],
            )
            core = {
                key: value
                for key, value in first_model.items()
                if key != "content_address"
            }
            self.assertEqual(
                first_model["content_address"]["sha256"], _sha256_json(core)
            )
            self.assertEqual(first.model.read_bytes(), first.content_addressed_model.read_bytes())
            evaluation_core = {
                key: value
                for key, value in first_evaluation.items()
                if key != "content_address"
            }
            self.assertEqual(
                first_evaluation["content_address"]["sha256"],
                _sha256_json(evaluation_core),
            )
            self.assertEqual(
                first.evaluation.read_bytes(),
                first.content_addressed_evaluation.read_bytes(),
            )
            self.assertEqual(first.comparison_status, "NOT_COMPARISON_READY")
            self.assertFalse(first_model["claim_boundary"]["fourth_baseline_ready"])
            self.assertFalse(first_model["claim_boundary"]["arms_can_vote"])

    def test_component_heldout_split_is_disjoint_and_exhaustive(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            manifest, _ = _fixture(root)
            result = build_chronicle_historical_warrior_policy(
                team_wave_model_manifest_path=manifest,
                output_directory=root / "out",
            )
            evaluation = _load(result.evaluation)
            assignments = evaluation["split"]["component_to_fold"]
            self.assertEqual(len(assignments), 12)
            self.assertEqual(len({row["component_id"] for row in assignments}), 12)
            for lane in (FURY_LANE, ARMS_LANE):
                seen: list[str] = []
                for fold in evaluation["lanes"][lane]["folds"]:
                    train = set(fold["train_component_ids"])
                    test = set(fold["test_component_ids"])
                    self.assertTrue(fold["component_sets_disjoint"])
                    self.assertFalse(train.intersection(test))
                    seen.extend(test)
                self.assertEqual(len(seen), 12)
                self.assertEqual(len(set(seen)), 12)

    def test_contamination_spec_and_named_player_rules_are_exact(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            manifest, _ = _fixture(root)
            result = build_chronicle_historical_warrior_policy(
                team_wave_model_manifest_path=manifest,
                output_directory=root / "out",
            )
            model = _load(result.model)
            evaluation = _load(result.evaluation)
            self.assertEqual(result.fury_decision_count, 20)
            self.assertEqual(result.arms_decision_count, 20)
            fury_accounting = evaluation["data_accounting"]["decision_counts_by_lane"][FURY_LANE]
            self.assertEqual(fury_accounting["POSTFIX_KNOWN_CLEAN"], 4)
            self.assertEqual(fury_accounting["NO_KNOWN_RULE_MATCH"], 16)
            self.assertNotIn("SUSPECT_36YD_RANGE_BUG", fury_accounting)
            self.assertNotIn("UNKNOWN_NONVOTING", fury_accounting)
            excluded = evaluation["data_accounting"]["excluded_episode_reasons_by_lane"]
            for lane in (FURY_LANE, ARMS_LANE):
                self.assertEqual(
                    excluded[lane]["CONTAMINATION_SUSPECT_36YD_RANGE_BUG"], 1
                )
                self.assertEqual(
                    excluded[lane]["CONTAMINATION_UNKNOWN_NONVOTING"], 1
                )
            serialized = json.dumps(model, ensure_ascii=False, sort_keys=True)
            self.assertNotIn("托尼牛", serialized)
            self.assertNotIn("桃姬儿", serialized)
            self.assertNotIn("999999", serialized)
            self.assertNotIn("777777", serialized)
            self.assertFalse(
                model["training_contract"]["named_players_receive_permanent_weight_changes"]
            )
            self.assertEqual(
                model["lanes"][ARMS_LANE]["policy_role"],
                "COMMON_ACTION_DIAGNOSTIC_ONLY_NONVOTING",
            )

    def test_fury_and_arms_models_never_share_action_counts(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            manifest, _ = _fixture(root)
            result = build_chronicle_historical_warrior_policy(
                team_wave_model_manifest_path=manifest,
                output_directory=root / "out",
            )
            model = _load(result.model)
            fury = set(model["lanes"][FURY_LANE]["fitted_policy"]["action_catalog"])
            arms = set(model["lanes"][ARMS_LANE]["fitted_policy"]["action_catalog"])
            self.assertTrue(fury)
            self.assertTrue(arms)
            self.assertFalse(fury.intersection(arms))
            self.assertTrue(any("id:23881" in action for action in fury))
            self.assertTrue(any("id:12294" in action for action in arms))

    def test_start_cast_pair_is_causally_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            manifest, _ = _fixture(root)
            result = build_chronicle_historical_warrior_policy(
                team_wave_model_manifest_path=manifest,
                output_directory=root / "out",
            )
            evaluation = _load(result.evaluation)
            for lane in (FURY_LANE, ARMS_LANE):
                audit = evaluation["data_accounting"]["action_label_audit_by_lane"][lane]
                self.assertEqual(audit["paired_casts_deduplicated_from_prior_start"], 10)
                self.assertEqual(audit["start_action_labels"], 10)
                self.assertEqual(audit["instant_or_unpaired_cast_action_labels"], 10)
                self.assertEqual(audit["accepted_action_labels"], 20)

    def test_synthetic_data_cannot_pass_full_data_comparison_gate(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            manifest, _ = _fixture(root)
            result = build_chronicle_historical_warrior_policy(
                team_wave_model_manifest_path=manifest,
                output_directory=root / "out",
            )
            evaluation = _load(result.evaluation)
            metrics = evaluation["lanes"][FURY_LANE]["metrics"]
            self.assertIsNotNone(metrics["known_action_coverage"])
            self.assertIsNotNone(metrics["calibration"]["expected_calibration_error"])
            self.assertEqual(
                evaluation["fury_internal_fidelity_gate"]["status"],
                "INTERNAL_HELDOUT_FIDELITY_FAIL",
            )
            reasons = evaluation["claim_boundary"]["rejection_reasons"]
            self.assertIn("FULL_DATA_HELDOUT_FIDELITY_GATE_NOT_PASSED", reasons)
            self.assertIn("INSUFFICIENT_HELDOUT_DECISIONS", reasons)
            self.assertIn("DOWNSTREAM_SIMULATOR_RUNTIME_ADMISSION_NOT_VALIDATED", reasons)
            self.assertEqual(
                evaluation["claim_boundary"]["comparison_status"],
                "NOT_COMPARISON_READY",
            )

    def test_runtime_distribution_and_sampling_only_emit_legal_actions(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            manifest, _ = _fixture(root)
            result = build_chronicle_historical_warrior_policy(
                team_wave_model_manifest_path=manifest,
                output_directory=root / "out",
            )
            state = _runtime_state()
            legal_actions = [
                {
                    "candidate_id": "bloodthirst",
                    "action": {
                        "spell": {"id": 23881, "name": "Bloodthirst"},
                        "target": {"guid": TARGET},
                    },
                },
                {
                    "candidate_id": "whirlwind",
                    "action": {
                        "spell": {"id": 1680, "name": "Whirlwind"},
                        "target": {"guid": TARGET},
                    },
                },
                {
                    "candidate_id": "novel-slam",
                    "action": {
                        "spell": {"id": 1464, "name": "Slam"},
                        "target": {"guid": TARGET},
                    },
                },
                {
                    "candidate_id": "dead-target",
                    "action": {
                        "spell": {"id": 23881, "name": "Bloodthirst"},
                        "target": {"guid": DEAD_TARGET},
                    },
                },
                {
                    "candidate_id": "explicitly-illegal",
                    "legal": False,
                    "action": {
                        "spell": {"id": 999, "name": "Illegal"},
                        "target": {"guid": TARGET},
                    },
                },
            ]
            distribution = predict_action_distribution(
                result.model,
                state_before=state,
                legal_actions=legal_actions,
            )
            rows = distribution["candidate_distribution"]
            self.assertEqual(
                {row["candidate_id"] for row in rows},
                {"bloodthirst", "whirlwind", "novel-slam"},
            )
            self.assertAlmostEqual(sum(row["probability"] for row in rows), 1.0)
            novel = next(row for row in rows if row["candidate_id"] == "novel-slam")
            self.assertGreater(novel["probability"], 0)
            first = sample_legal_action(
                result.model,
                state_before=state,
                legal_actions=legal_actions,
                seed={"sim_seed": 991, "state": "new"},
            )
            second = sample_legal_action(
                result.model,
                state_before=state,
                legal_actions=legal_actions,
                seed={"sim_seed": 991, "state": "new"},
            )
            self.assertEqual(first, second)
            self.assertIn(
                first["candidate_id"], {"bloodthirst", "whirlwind", "novel-slam"}
            )
            self.assertEqual(first["comparison_status"], "NOT_COMPARISON_READY")

    def test_future_state_key_is_rejected_in_training_and_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            manifest, _ = _fixture(root, future_key=True)
            with self.assertRaisesRegex(
                HistoricalWarriorPolicyError, "forbidden future/outcome feature"
            ):
                build_chronicle_historical_warrior_policy(
                    team_wave_model_manifest_path=manifest,
                    output_directory=root / "out",
                )
            clean_manifest, _ = _fixture(root / "clean")
            result = build_chronicle_historical_warrior_policy(
                team_wave_model_manifest_path=clean_manifest,
                output_directory=root / "clean-out",
            )
            with self.assertRaisesRegex(
                HistoricalWarriorPolicyError, "forbidden future/outcome feature"
            ):
                predict_action_distribution(
                    result.model,
                    state_before=_runtime_state(future_key=True),
                    legal_actions=[
                        {
                            "spell": {"id": 23881, "name": "Bloodthirst"},
                            "target": {"guid": TARGET},
                        }
                    ],
                )

    def test_transition_and_partition_integrity_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            bad_manifest, _ = _fixture(root / "transition", bad_transition_hash=True)
            with self.assertRaisesRegex(
                HistoricalWarriorPolicyError, "transition content hash mismatch"
            ):
                build_chronicle_historical_warrior_policy(
                    team_wave_model_manifest_path=bad_manifest,
                    output_directory=root / "bad-out",
                )
            manifest, partition = _fixture(root / "partition")
            payload = bytearray(partition.read_bytes())
            payload[len(payload) // 2] ^= 1
            partition.write_bytes(payload)
            with self.assertRaisesRegex(
                HistoricalWarriorPolicyError, "partition compressed SHA-256 mismatch"
            ):
                build_chronicle_historical_warrior_policy(
                    team_wave_model_manifest_path=manifest,
                    output_directory=root / "tampered-out",
                )


if __name__ == "__main__":
    unittest.main()
