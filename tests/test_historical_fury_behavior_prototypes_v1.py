from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from fractions import Fraction
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from o2o_dps import historical_behavior_clone_v1 as pooled_v1
from o2o_dps import historical_fury_behavior_prototypes_v1 as prototypes_v1
from o2o_dps import historical_fury_expert_cohort_v2 as cohort_v2
from o2o_dps import historical_fury_expert_episode_adapter_v1 as episode_v1


RAID_A = "11111111-1111-1111-1111-111111111111"
RAID_B = "22222222-2222-2222-2222-222222222222"
RAID_C = "33333333-3333-3333-3333-333333333333"


def _canonical(value: object, *, newline: bool = False) -> bytes:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return payload + (b"\n" if newline else b"")


def _candidate(
    ordinal: int,
    ratios: list[float],
    *,
    raids: list[str] | None = None,
) -> dict[str, object]:
    guid = f"0x{ordinal:016X}"
    raid_ids = raids or ([RAID_A, RAID_B] if len(ratios) == 2 else [RAID_C])
    ordered = sorted(ratios)
    middle = len(ordered) // 2
    median = (
        ordered[middle]
        if len(ordered) % 2
        else (ordered[middle - 1] + ordered[middle]) / 2.0
    )
    return {
        "candidate_id": f"player:{guid.casefold()}",
        "character_guid": guid,
        "latest_character_name": f"玩家{ordinal}",
        "raid_count": len(raid_ids),
        "encounter_observation_count": len(raid_ids),
        "median_equal_raid_local_fury_dps_ratio": median,
        "right_censored_after_last_observed_raid": True,
        "raids": [
            {
                "instance_id": instance_id,
                "median_local_fury_dps_ratio": ratio,
                "encounter_observation_count": 1,
            }
            for instance_id, ratio in zip(raid_ids, ratios)
        ],
    }


def _cohort() -> dict[str, object]:
    # Comparable-player medians are .7, .8, .9, 1.0, 1.15, 1.2, 1.3, 1.4.
    # Inclusive nearest-rank Q75 is 1.2, so players 2/3/4 form Q4.  Players
    # 1--4 all exceed 1.0 in every one of two raids and are the stable four.
    return {
        "schema": cohort_v2.SCHEMA,
        "implementation_revision": cohort_v2.IMPLEMENTATION_REVISION,
        "player_candidates": [
            _candidate(1, [1.1, 1.2]),
            _candidate(2, [1.1, 1.3]),
            _candidate(3, [1.2, 1.4]),
            _candidate(4, [1.3, 1.5]),
            _candidate(5, [0.7]),
            _candidate(6, [0.8]),
            _candidate(7, [0.9]),
            _candidate(8, [1.0]),
        ],
        "summary": {
            "selected_exact_fury_encounter_observation_count": 12,
        },
    }


def _transition(
    *,
    phase: str,
    timestamp_ms: int,
    action_key: str,
    policy_label: bool,
) -> dict[str, object]:
    return {
        "trace_index": timestamp_ms,
        "state_before": {
            "cutoff_semantics": "strictly before current event order_key",
            "cutoff_exclusive_order_key": [timestamp_ms, timestamp_ms, 0, 0],
            "wave_elapsed_ms": timestamp_ms - 1_000,
            "prefix_merged_event_count": 0,
        },
        "observed_event": {
            "phase": phase,
            "learning_role": (
                "SERVER_OBSERVED_START_CONTROLLABLE_ACTION_PROXY"
                if policy_label
                else "ACTION_RESULT_OR_UNCLASSIFIED_START"
            ),
            "action_key": action_key,
            "action_lane": "gcd" if "unmapped" not in action_key else "unmapped",
            "ontology_status": (
                "KNOWN_HISTORICAL_FURY_V3_CONTROLLABLE_ACTION"
                if "unmapped" not in action_key
                else "OUTSIDE_HISTORICAL_FURY_V3_ONTOLOGY_PRESERVED"
            ),
            "server_observed_start_proxy": phase == "START",
            "client_action_request_observed": False,
            "client_next_swing_queue_intent_observed": False,
            "policy_decision_label": policy_label,
            "order_key": [timestamp_ms, timestamp_ms, 0, 0],
            "anchor": {"timestamp_ms": timestamp_ms, "offset_ms": timestamp_ms - 1_000},
            "spell": {"id": 23894, "name": "Bloodthirst"},
            "exact_target": {
                "guid": "0xF130000000000001",
                "lane": "HOSTILE_CREATURE",
                "voting_enemy_target": True,
            },
            "source_guid": None,
            "action_payload": {"phase": phase},
        },
        "feature_cutoff_is_strict_prefix": True,
        "current_event_present_in_state_before": False,
        "future_outcomes_in_state_before": False,
    }


def _episode(
    guid: str,
    instance_id: str,
    start_count: int,
    *,
    bad_go_label: bool = False,
    left_unobserved_ms: int = 0,
    wave_ordinal: int = 1,
) -> dict[str, object]:
    transitions = [
        _transition(
            phase="START",
            timestamp_ms=1_100 + index * 100,
            action_key=(
                "warrior.bloodthirst" if index % 2 == 0 else "warrior.whirlwind"
            ),
            policy_label=True,
        )
        for index in range(start_count)
    ]
    transitions.extend(
        [
            _transition(
                phase="GO",
                timestamp_ms=2_500,
                action_key="warrior.bloodthirst",
                policy_label=bad_go_label,
            ),
            _transition(
                phase="FAIL",
                timestamp_ms=2_600,
                action_key="warrior.bloodthirst",
                policy_label=False,
            ),
            _transition(
                phase="START",
                timestamp_ms=2_800,
                action_key="unmapped.spell_id.99999",
                policy_label=False,
            ),
        ]
    )
    return {
        "schema": episode_v1.SCHEMA,
        "implementation_revision": episode_v1.IMPLEMENTATION_REVISION,
        "record_type": "historical_fury_exact_window_observation_episode",
        "status": episode_v1.STATUS,
        "episode_id": f"{instance_id}:{guid}",
        "instance_id": instance_id,
        "encounter_id": f"encounter-{instance_id}",
        "player": {
            "guid": guid,
            "name": f"name-{guid}",
            "class": "WARRIOR",
            "window_level_spec": "Fury",
        },
        "exact_dps_window": {"ranking_record_id": f"ranking-{instance_id}-{guid}"},
        "window_join": {
            "coverage": {
                "partial": left_unobserved_ms > 0,
                "left_unobserved_ms": left_unobserved_ms,
                "right_unobserved_ms": 0,
                "wave_envelope_exactly_covers_metadata_window": True,
            }
        },
        "wave_observations": [
            {
                "wave_id": f"wave-{instance_id}-{guid}",
                "wave_ordinal": wave_ordinal,
                "encounter_ordinal": 1,
                "source_wave_content_sha256": "a" * 64,
                "window": {
                    "first_anchor": {"timestamp_ms": 1_000},
                    "last_context_anchor": {"timestamp_ms": 3_000},
                },
                "prefix_transitions": transitions,
                "summary": {},
            }
        ],
    }


def _write_partition(
    directory: Path, instance_id: str, episodes: list[dict[str, object]]
) -> dict[str, object]:
    logical = b"".join(_canonical(episode, newline=True) for episode in episodes)
    path = directory / f"{instance_id}.jsonl.gz"
    with path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            compressed.write(logical)
    starts = 0
    labels = 0
    complete = 0
    partial = 0
    left_unobserved = 0
    right_unobserved = 0
    for episode in episodes:
        coverage = episode["window_join"]["coverage"]
        if coverage["partial"]:
            partial += 1
        else:
            complete += 1
        left_unobserved += coverage["left_unobserved_ms"]
        right_unobserved += coverage["right_unobserved_ms"]
        for wave in episode["wave_observations"]:
            for transition in wave["prefix_transitions"]:
                observed = transition["observed_event"]
                starts += int(observed["phase"] == "START")
                labels += int(observed["policy_decision_label"] is True)
    return {
        "instance_id": instance_id,
        "path": path.name,
        "record_schema": episode_v1.SCHEMA,
        "record_count": len(episodes),
        "logical_size_bytes": len(logical),
        "logical_content_sha256": hashlib.sha256(logical).hexdigest(),
        "server_observed_start_count": starts,
        "controllable_policy_label_count": labels,
        "unknown_or_conflicting_timeline_player_wave_count": 0,
        "complete_wave_coverage_episode_count": complete,
        "partial_wave_coverage_episode_count": partial,
        "left_unobserved_ms_across_episodes": left_unobserved,
        "right_unobserved_ms_across_episodes": right_unobserved,
    }


def _write_manifest_pair(path: Path, core: dict[str, object]) -> None:
    manifest = deepcopy(core)
    manifest.pop("content_address", None)
    address = hashlib.sha256(_canonical(manifest)).hexdigest()
    manifest["content_address"] = {
        "algorithm": "sha256",
        "scope": "canonical JSON excluding content_address",
        "sha256": address,
    }
    payload = _canonical(manifest, newline=True)
    path.write_bytes(payload)
    addressed = path.parent / (
        f"historical_fury_expert_episode_adapter_v1.{address}.manifest.json"
    )
    addressed.write_bytes(payload)


def _fixture(
    root: Path,
    *,
    bad_go_label: bool = False,
    duplicate_episode: bool = False,
    first_selected_left_unobserved: bool = False,
    first_selected_wave_ordinal: int = 1,
    append_unusable_second_wave: bool = False,
) -> tuple[Path, Path, Path]:
    data = root / "offline_data"
    episode_dir = data / "derived" / "episodes"
    episode_dir.mkdir(parents=True)
    cohort_path = data / "derived" / "cohort.json"
    cohort_path.write_bytes(_canonical(_cohort(), newline=True))

    counts = {
        1: (1, 1),
        2: (1, 3),
        3: (2, 1),
        4: (1, 1),
    }
    partitions = []
    for raid_index, instance_id in enumerate((RAID_A, RAID_B)):
        episodes = []
        for ordinal in range(1, 5):
            guid = f"0x{ordinal:016X}"
            episode = _episode(
                guid,
                instance_id,
                counts[ordinal][raid_index],
                bad_go_label=bad_go_label and ordinal == 1 and raid_index == 0,
                left_unobserved_ms=(
                    100
                    if first_selected_left_unobserved
                    and ordinal == 1
                    and raid_index == 0
                    else 0
                ),
                wave_ordinal=(
                    first_selected_wave_ordinal
                    if ordinal == 1 and raid_index == 0
                    else 1
                ),
            )
            if append_unusable_second_wave and ordinal == 1 and raid_index == 0:
                second_wave = deepcopy(episode["wave_observations"][0])
                second_wave["wave_id"] += "-second"
                second_wave["wave_ordinal"] = 2
                second_wave["source_wave_content_sha256"] = "c" * 64
                second_wave["prefix_transitions"] = [
                    transition
                    for transition in second_wave["prefix_transitions"]
                    if transition["observed_event"]["policy_decision_label"] is not True
                ]
                episode["wave_observations"].append(second_wave)
            episodes.append(episode)
        if duplicate_episode and raid_index == 0:
            episodes.append(deepcopy(episodes[0]))
        partitions.append(_write_partition(episode_dir, instance_id, episodes))
    partitions.append(
        _write_partition(
            episode_dir,
            RAID_C,
            [
                _episode(f"0x{ordinal:016X}", RAID_C, 1)
                for ordinal in range(5, 9)
            ],
        )
    )
    episode_count = sum(partition["record_count"] for partition in partitions)
    start_count = sum(
        partition["server_observed_start_count"] for partition in partitions
    )
    label_count = sum(
        partition["controllable_policy_label_count"] for partition in partitions
    )
    complete_count = sum(
        partition["complete_wave_coverage_episode_count"] for partition in partitions
    )
    partial_count = sum(
        partition["partial_wave_coverage_episode_count"] for partition in partitions
    )
    episode_manifest = {
        "schema": episode_v1.MANIFEST_SCHEMA,
        "implementation_revision": episode_v1.IMPLEMENTATION_REVISION,
        "status": episode_v1.STATUS,
        "input_closure": {
            "frozen_cohort": {
                "schema": cohort_v2.SCHEMA,
                "file_sha256": hashlib.sha256(cohort_path.read_bytes()).hexdigest(),
            }
        },
        "partitions": partitions,
        "unresolved_observations": [],
        "summary": {
            "candidate_nonnull_encounter_episode_count": episode_count,
            "selected_exact_fury_observation_count": episode_count,
            "unresolved_null_encounter_observation_count": 0,
            "instance_partition_count": len(partitions),
            "server_observed_start_count": start_count,
            "controllable_policy_label_count": label_count,
            "complete_wave_coverage_episode_count": complete_count,
            "partial_wave_coverage_episode_count": partial_count,
            "unknown_or_conflicting_timeline_player_wave_count": 0,
            "maximum_left_unobserved_ms": (
                100 if first_selected_left_unobserved else 0
            ),
            "maximum_right_unobserved_ms": 0,
        },
        "scientific_boundaries": {
            "comparison_authorized": False,
            "training_authorized": False,
            "closed_loop_baseline_authorized": False,
            "superiority_claim_authorized": False,
        },
    }
    episode_path = episode_dir / "manifest.json"
    _write_manifest_pair(episode_path, episode_manifest)
    output = data / "derived" / "prototypes"
    return cohort_path, episode_path, output


def _read_partition(manifest_path: Path, row: dict[str, object]) -> list[dict]:
    path = manifest_path.parent / str(row["partition"]["path"])
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


class HistoricalFuryBehaviorPrototypesV1Tests(unittest.TestCase):
    def test_memberships_are_per_raid_derived_and_name_independent(self) -> None:
        cohort = _cohort()
        prototypes, contract = prototypes_v1.derive_prototype_memberships_v1(cohort)
        stable = [
            row
            for row in prototypes
            if row["prototype_family"] == "PLAYER_CONDITIONED_STABLE_REPEAT_RAID"
        ]
        pooled = next(
            row
            for row in prototypes
            if row["prototype_id"] == "player_equal_q4_pooled"
        )

        self.assertEqual(5, len(prototypes))
        self.assertEqual(4, len(stable))
        self.assertEqual(
            {f"player:0x{ordinal:016x}" for ordinal in (2, 3, 4)},
            {member["candidate_id"] for member in pooled["members"]},
        )
        self.assertAlmostEqual(1.2, contract["q4_cutoff"])
        self.assertFalse(contract["names_used_for_selection"])

        for ordinal, candidate in enumerate(cohort["player_candidates"], 1):
            candidate["latest_character_name"] = f"完全不同的名字{ordinal}"
        renamed, _ = prototypes_v1.derive_prototype_memberships_v1(cohort)
        self.assertEqual(
            [row["prototype_id"] for row in prototypes],
            [row["prototype_id"] for row in renamed],
        )
        self.assertEqual(
            [
                [member["candidate_id"] for member in row["members"]]
                for row in prototypes
            ],
            [
                [member["candidate_id"] for member in row["members"]]
                for row in renamed
            ],
        )

    def test_streamed_artifact_closes_hierarchical_start_weights(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fury_prototypes_") as raw:
            cohort_path, episode_path, output = _fixture(Path(raw))
            with mock.patch.object(
                prototypes_v1.cohort_v2, "validate_cohort_document", return_value={}
            ):
                result = prototypes_v1.build_historical_fury_behavior_prototypes_v1(
                    cohort_path=cohort_path,
                    episode_manifest_path=episode_path,
                    output_directory=output,
                )
            manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
            pooled = next(
                row
                for row in manifest["prototypes"]
                if row["prototype_id"] == "player_equal_q4_pooled"
            )
            pooled_records = _read_partition(result.manifest, pooled)

            self.assertEqual(5, result.prototype_count)
            self.assertEqual(20, result.prepared_start_observation_count)
            self.assertEqual(14, result.right_censored_tail_count)
            self.assertEqual(2, manifest["input_closure"]["streaming_pass_count"])
            self.assertEqual(0, manifest["input_closure"]["episode_rows_retained_in_memory"])
            self.assertFalse(manifest["scientific_boundaries"]["training_authorized"])
            self.assertFalse(manifest["scientific_boundaries"]["comparison_authorized"])
            self.assertFalse(
                manifest["scientific_boundaries"][
                    "same_equipment_matched_seed_comparison_authorized"
                ]
            )
            self.assertEqual(pooled_v1.COHORT_ID, manifest["external_controls"][0]["control_id"])
            self.assertFalse(
                manifest["external_controls"][0]["materialized_by_this_artifact"]
            )

            starts = [
                row
                for row in pooled_records
                if row["record_type"] == "weighted_server_observed_start"
            ]
            tails = [
                row
                for row in pooled_records
                if row["record_type"] == "right_censored_wave_tail"
            ]
            self.assertEqual(9, len(starts))
            self.assertEqual(6, len(tails))
            self.assertTrue(
                all(row["observed_start"]["phase"] == "START" for row in starts)
            )
            self.assertTrue(
                all(
                    row["delay_observation_status"]
                    == prototypes_v1.DELAY_COMPLETED
                    for row in starts
                )
            )
            self.assertTrue(
                all(
                    row["observation_boundaries"]["go_fail_are_labels"] is False
                    and row["observation_boundaries"][
                        "client_next_swing_queue_intent"
                    ]
                    == prototypes_v1.QUEUE_INTENT_STATUS
                    for row in starts
                )
            )

            raid_mass: defaultdict[tuple[str, str], Fraction] = defaultdict(Fraction)
            player_mass: defaultdict[str, Fraction] = defaultdict(Fraction)
            for row in starts:
                weight = row["prepared_training_weight"]
                value = Fraction(weight["numerator"], weight["denominator"])
                guid = row["source"]["player_guid"]
                instance_id = row["source"]["instance_id"]
                raid_mass[(guid, instance_id)] += value
                player_mass[guid] += value
            self.assertEqual({Fraction(1, 3)}, set(player_mass.values()))
            self.assertEqual({Fraction(1, 6)}, set(raid_mass.values()))
            self.assertEqual(
                {"numerator": 1, "denominator": 1, "value": 1.0},
                pooled["partition"]["prepared_start_weight_mass"],
            )
            self.assertTrue(all(row["prepared_training_weight"] is None for row in tails))
            self.assertTrue(
                all(
                    row["delay_observation_status"] == "RIGHT_CENSORED"
                    for row in tails
                )
            )
            self.assertTrue(
                all(
                    row["right_censored_tail"]["right_censored"] is True
                    and row["right_censored_tail"][
                        "preceding_recognized_controllable_start"
                    ]["policy_decision_label"]
                    is True
                    for row in tails
                )
            )
            self.assertEqual(
                manifest["summary"]["prepared_start_observation_count"],
                manifest["summary"]["completed_delay_count"],
            )
            self.assertEqual(0, manifest["summary"]["left_truncated_first_start_count"])
            self.assertEqual(
                manifest["summary"]["right_censored_tail_count"],
                manifest["summary"]["usable_right_censor_count"],
            )
            self.assertEqual(
                0,
                manifest["summary"][
                    "unusable_left_right_truncated_tail_count"
                ],
            )

    def test_episode_manifest_content_address_is_verified(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fury_prototypes_address_") as raw:
            cohort_path, episode_path, output = _fixture(Path(raw))
            manifest = json.loads(episode_path.read_text(encoding="utf-8"))
            manifest["content_address"]["sha256"] = "b" * 64
            episode_path.write_bytes(_canonical(manifest, newline=True))
            with mock.patch.object(
                prototypes_v1.cohort_v2,
                "validate_cohort_document",
                return_value={},
            ), self.assertRaisesRegex(
                prototypes_v1.HistoricalFuryBehaviorPrototypesV1Error,
                "content_address does not bind",
            ):
                prototypes_v1.build_historical_fury_behavior_prototypes_v1(
                    cohort_path=cohort_path,
                    episode_manifest_path=episode_path,
                    output_directory=output,
                )

    def test_stable_and_addressed_episode_manifest_bytes_must_agree(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fury_prototypes_pair_") as raw:
            cohort_path, episode_path, output = _fixture(Path(raw))
            manifest = json.loads(episode_path.read_text(encoding="utf-8"))
            address = manifest["content_address"]["sha256"]
            addressed = episode_path.parent / (
                "historical_fury_expert_episode_adapter_v1."
                f"{address}.manifest.json"
            )
            addressed.write_bytes(addressed.read_bytes() + b"\n")
            with mock.patch.object(
                prototypes_v1.cohort_v2,
                "validate_cohort_document",
                return_value={},
            ), self.assertRaisesRegex(
                prototypes_v1.HistoricalFuryBehaviorPrototypesV1Error,
                "stable and content-addressed.*bytes differ",
            ):
                prototypes_v1.build_historical_fury_behavior_prototypes_v1(
                    cohort_path=cohort_path,
                    episode_manifest_path=episode_path,
                    output_directory=output,
                )

    def test_duplicate_global_episode_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fury_prototypes_duplicate_") as raw:
            cohort_path, episode_path, output = _fixture(
                Path(raw), duplicate_episode=True
            )
            with mock.patch.object(
                prototypes_v1.cohort_v2,
                "validate_cohort_document",
                return_value={},
            ), self.assertRaisesRegex(
                prototypes_v1.HistoricalFuryBehaviorPrototypesV1Error,
                "duplicate global episode_id",
            ):
                prototypes_v1.build_historical_fury_behavior_prototypes_v1(
                    cohort_path=cohort_path,
                    episode_manifest_path=episode_path,
                    output_directory=output,
                )

    def test_episode_aggregate_must_match_streamed_rows(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fury_prototypes_summary_") as raw:
            cohort_path, episode_path, output = _fixture(Path(raw))
            manifest = json.loads(episode_path.read_text(encoding="utf-8"))
            manifest["summary"]["candidate_nonnull_encounter_episode_count"] -= 1
            _write_manifest_pair(episode_path, manifest)
            with mock.patch.object(
                prototypes_v1.cohort_v2,
                "validate_cohort_document",
                return_value={},
            ), self.assertRaisesRegex(
                prototypes_v1.HistoricalFuryBehaviorPrototypesV1Error,
                "aggregate candidate_nonnull_encounter_episode_count differs",
            ):
                prototypes_v1.build_historical_fury_behavior_prototypes_v1(
                    cohort_path=cohort_path,
                    episode_manifest_path=episode_path,
                    output_directory=output,
                )

    def test_episode_summary_and_unresolved_rows_are_required(self) -> None:
        for field, expected_error in (
            ("summary", "episode manifest summary must be an object"),
            (
                "unresolved_observations",
                "episode unresolved_observations must be an array",
            ),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory(
                prefix="fury_prototypes_required_closure_"
            ) as raw:
                cohort_path, episode_path, output = _fixture(Path(raw))
                manifest = json.loads(episode_path.read_text(encoding="utf-8"))
                del manifest[field]
                _write_manifest_pair(episode_path, manifest)
                with mock.patch.object(
                    prototypes_v1.cohort_v2,
                    "validate_cohort_document",
                    return_value={},
                ), self.assertRaisesRegex(
                    prototypes_v1.HistoricalFuryBehaviorPrototypesV1Error,
                    expected_error,
                ):
                    prototypes_v1.build_historical_fury_behavior_prototypes_v1(
                        cohort_path=cohort_path,
                        episode_manifest_path=episode_path,
                        output_directory=output,
                    )

    def test_removing_partition_cannot_reclose_against_frozen_cohort(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fury_prototypes_subset_") as raw:
            cohort_path, episode_path, output = _fixture(Path(raw))
            manifest = json.loads(episode_path.read_text(encoding="utf-8"))
            removed = next(
                row for row in manifest["partitions"] if row["instance_id"] == RAID_C
            )
            manifest["partitions"].remove(removed)
            summary = manifest["summary"]
            summary["candidate_nonnull_encounter_episode_count"] -= removed[
                "record_count"
            ]
            summary["selected_exact_fury_observation_count"] -= removed[
                "record_count"
            ]
            summary["instance_partition_count"] -= 1
            summary["server_observed_start_count"] -= removed[
                "server_observed_start_count"
            ]
            summary["controllable_policy_label_count"] -= removed[
                "controllable_policy_label_count"
            ]
            summary["complete_wave_coverage_episode_count"] -= removed[
                "complete_wave_coverage_episode_count"
            ]
            summary["partial_wave_coverage_episode_count"] -= removed[
                "partial_wave_coverage_episode_count"
            ]
            summary[
                "unknown_or_conflicting_timeline_player_wave_count"
            ] -= removed["unknown_or_conflicting_timeline_player_wave_count"]
            _write_manifest_pair(episode_path, manifest)
            with mock.patch.object(
                prototypes_v1.cohort_v2,
                "validate_cohort_document",
                return_value={},
            ), self.assertRaisesRegex(
                prototypes_v1.HistoricalFuryBehaviorPrototypesV1Error,
                "no longer closes to the frozen Fury cohort",
            ):
                prototypes_v1.build_historical_fury_behavior_prototypes_v1(
                    cohort_path=cohort_path,
                    episode_manifest_path=episode_path,
                    output_directory=output,
                )

    def test_first_start_delay_is_left_truncated_by_coverage_or_wave_split(self) -> None:
        cases = (
            {"first_selected_left_unobserved": True},
            {"first_selected_wave_ordinal": 2},
        )
        for fixture_options in cases:
            with self.subTest(fixture_options=fixture_options), tempfile.TemporaryDirectory(
                prefix="fury_prototypes_left_start_"
            ) as raw:
                cohort_path, episode_path, output = _fixture(
                    Path(raw), **fixture_options
                )
                with mock.patch.object(
                    prototypes_v1.cohort_v2,
                    "validate_cohort_document",
                    return_value={},
                ):
                    result = prototypes_v1.build_historical_fury_behavior_prototypes_v1(
                        cohort_path=cohort_path,
                        episode_manifest_path=episode_path,
                        output_directory=output,
                    )
                manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
                prototype = next(
                    row
                    for row in manifest["prototypes"]
                    if row["prototype_family"]
                    == "PLAYER_CONDITIONED_STABLE_REPEAT_RAID"
                    and row["members"][0]["character_guid"]
                    == "0x0000000000000001"
                )
                records = _read_partition(result.manifest, prototype)
                first = next(
                    row
                    for row in records
                    if row["record_type"] == "weighted_server_observed_start"
                    and row["source"]["instance_id"] == RAID_A
                )
                self.assertEqual(
                    prototypes_v1.DELAY_LEFT_TRUNCATED,
                    first["delay_observation_status"],
                )
                raid_support = next(
                    row
                    for row in prototype["observation_support"][0]["raids"]
                    if row["instance_id"] == RAID_A
                )
                self.assertEqual(1, raid_support["left_truncated_first_start_count"])
                self.assertEqual(0, raid_support["completed_delay_count"])
                self.assertEqual(1, raid_support["usable_right_censor_count"])
                self.assertEqual(
                    0,
                    raid_support["unusable_left_right_truncated_tail_count"],
                )

    def test_left_truncated_wave_without_start_has_unusable_tail(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fury_prototypes_left_tail_") as raw:
            cohort_path, episode_path, output = _fixture(
                Path(raw), append_unusable_second_wave=True
            )
            with mock.patch.object(
                prototypes_v1.cohort_v2,
                "validate_cohort_document",
                return_value={},
            ):
                result = prototypes_v1.build_historical_fury_behavior_prototypes_v1(
                    cohort_path=cohort_path,
                    episode_manifest_path=episode_path,
                    output_directory=output,
                )
            manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
            prototype = next(
                row
                for row in manifest["prototypes"]
                if row["prototype_family"]
                == "PLAYER_CONDITIONED_STABLE_REPEAT_RAID"
                and row["members"][0]["character_guid"]
                == "0x0000000000000001"
            )
            tails = [
                row
                for row in _read_partition(result.manifest, prototype)
                if row["record_type"] == "right_censored_wave_tail"
                and row["source"]["instance_id"] == RAID_A
            ]
            self.assertEqual(2, len(tails))
            self.assertEqual(
                {"RIGHT_CENSORED", "LEFT_AND_RIGHT_TRUNCATED_NOT_USABLE"},
                {row["delay_observation_status"] for row in tails},
            )
            raid_support = next(
                row
                for row in prototype["observation_support"][0]["raids"]
                if row["instance_id"] == RAID_A
            )
            self.assertEqual(2, raid_support["wave_count"])
            self.assertEqual(1, raid_support["usable_right_censor_count"])
            self.assertEqual(
                1, raid_support["unusable_left_right_truncated_tail_count"]
            )

    def test_later_unmapped_start_does_not_shorten_interdecision_tail(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fury_prototypes_tail_") as raw:
            cohort_path, episode_path, output = _fixture(Path(raw))
            with mock.patch.object(
                prototypes_v1.cohort_v2, "validate_cohort_document", return_value={}
            ):
                result = prototypes_v1.build_historical_fury_behavior_prototypes_v1(
                    cohort_path=cohort_path,
                    episode_manifest_path=episode_path,
                    output_directory=output,
                )
            manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
            pooled = next(
                row
                for row in manifest["prototypes"]
                if row["prototype_id"] == "player_equal_q4_pooled"
            )
            tail = next(
                row
                for row in _read_partition(result.manifest, pooled)
                if row["record_type"] == "right_censored_wave_tail"
                and row["source"]["player_guid"] == "0x0000000000000002"
                and row["source"]["instance_id"] == RAID_A
            )["right_censored_tail"]

            # The recognized Bloodthirst START is at 1100 ms.  A later
            # unmapped START exists at 2800 ms, but is not a decision label.
            self.assertEqual("LAST_RECOGNIZED_CONTROLLABLE_START", tail["origin"])
            self.assertEqual(1_100, tail["origin_timestamp_ms"])
            self.assertEqual(1_900, tail["duration_ms"])
            self.assertEqual(
                "warrior.bloodthirst",
                tail["preceding_recognized_controllable_start"]["action_key"],
            )
            self.assertFalse(tail["later_unmapped_or_passive_starts_shorten_tail"])

    def test_go_outcome_cannot_be_promoted_to_label(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fury_prototypes_bad_go_") as raw:
            cohort_path, episode_path, output = _fixture(
                Path(raw), bad_go_label=True
            )
            with mock.patch.object(
                prototypes_v1.cohort_v2, "validate_cohort_document", return_value={}
            ):
                with self.assertRaisesRegex(
                    prototypes_v1.HistoricalFuryBehaviorPrototypesV1Error,
                    "GO/FAIL can never",
                ):
                    prototypes_v1.build_historical_fury_behavior_prototypes_v1(
                        cohort_path=cohort_path,
                        episode_manifest_path=episode_path,
                        output_directory=output,
                    )
            self.assertFalse((output / "manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
