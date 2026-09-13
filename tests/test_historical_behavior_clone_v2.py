from __future__ import annotations

from copy import deepcopy
from fractions import Fraction
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps import historical_behavior_clone_v2 as clone_v2
from o2o_dps import historical_fury_behavior_prototypes_v1 as prototypes_v1


PLAYER = "0x00000000000000A1"
RAID = "11111111-1111-1111-1111-111111111111"
TARGET = "0xF130000000000001"


def _wire(value: Fraction) -> dict[str, object]:
    return {
        "numerator": value.numerator,
        "denominator": value.denominator,
        "value": float(value),
    }


def _fraction(value: dict[str, object]) -> Fraction:
    return Fraction(int(value["numerator"]), int(value["denominator"]))


def _prototype() -> dict[str, object]:
    return {
        "prototype_id": "fixture_prototype",
        "prototype_family": "PLAYER_CONDITIONED_STABLE_REPEAT_RAID",
        "selection_rule": "fixture pre-simulator routing",
        "member_count": 1,
        "members": [
            {
                "candidate_id": "player:fixture",
                "character_guid": PLAYER,
                "latest_character_name": "Fixture",
                "raid_ids": [RAID],
                "raid_count": 1,
                "derived_median_equal_raid_local_fury_dps_ratio": 1.2,
                "per_raid_median_local_fury_dps_ratio": [
                    {"instance_id": RAID, "ratio": 1.2}
                ],
                "right_censored_after_last_observed_raid": True,
            }
        ],
        "observation_support": [
            {
                "candidate_id": "player:fixture",
                "character_guid": PLAYER,
                "prepared_player_weight_mass": _wire(Fraction(1, 1)),
                "raids": [
                    {
                        "instance_id": RAID,
                        "episode_count": 1,
                        "wave_count": 1,
                        "recognized_start_count": 2,
                        "completed_delay_count": 2,
                        "left_truncated_first_start_count": 0,
                        "usable_right_censor_count": 1,
                        "unusable_left_right_truncated_tail_count": 0,
                        "prepared_raid_weight_mass": _wire(Fraction(1, 1)),
                    }
                ],
            }
        ],
        "partition": {
            "record_schema": prototypes_v1.RECORD_SCHEMA,
            "prepared_start_observation_count": 2,
            "right_censored_tail_count": 1,
        },
    }


def _source() -> dict[str, object]:
    return {
        "episode_id": "episode-1",
        "instance_id": RAID,
        "encounter_id": "encounter-1",
        "ranking_record_id": "ranking-1",
        "player_guid": PLAYER,
        "wave_id": "wave-1",
        "wave_ordinal": 1,
        "encounter_ordinal": 1,
        "source_wave_content_sha256": "a" * 64,
        "episode_window_coverage": {
            "partial": False,
            "left_unobserved_ms": 0,
            "right_unobserved_ms": 0,
            "wave_envelope_exactly_covers_metadata_window": True,
        },
    }


def _boundaries() -> dict[str, object]:
    return {
        "policy_label_semantics": "recognized server-observed START proxy only",
        "go_fail_are_labels": False,
        "client_action_request_observed": False,
        "client_next_swing_queue_intent": prototypes_v1.QUEUE_INTENT_STATUS,
        "target_switch_intent": prototypes_v1.QUEUE_INTENT_STATUS,
        "strict_prefix": True,
        "actions_outside_reconstruction_waves": "MISSING_NOT_INFERRED",
    }


def _binding(prototype_id: str = "fixture_prototype") -> dict[str, object]:
    return {
        "prototype_manifest": {
            "schema": prototypes_v1.MANIFEST_SCHEMA,
            "implementation_revision": prototypes_v1.IMPLEMENTATION_REVISION,
            "file_sha256": "a" * 64,
            "content_sha256": "b" * 64,
        },
        "prototype_partition": {
            "record_schema": prototypes_v1.RECORD_SCHEMA,
            "implementation_revision": prototypes_v1.IMPLEMENTATION_REVISION,
            "record_count": 3,
            "logical_size_bytes": 1,
            "logical_content_sha256": "c" * 64,
        },
        "prototype_id": prototype_id,
        "network_request_count": 0,
    }


def _start(
    *,
    elapsed_ms: int,
    timestamp_ms: int,
    last_target: str | None,
    recent: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "schema": prototypes_v1.RECORD_SCHEMA,
        "implementation_revision": prototypes_v1.IMPLEMENTATION_REVISION,
        "record_type": "weighted_server_observed_start",
        "status": prototypes_v1.STATUS,
        "prototype_id": "fixture_prototype",
        "prototype_family": "PLAYER_CONDITIONED_STABLE_REPEAT_RAID",
        "source": _source(),
        "player_candidate_id": "player:fixture",
        "strict_prefix_state": {
            "cutoff_semantics": "strictly before current event order_key",
            "cutoff_exclusive_order_key": [timestamp_ms, timestamp_ms, 0, 0],
            "wave_elapsed_ms": elapsed_ms,
            "seen_hostile_target_guids": [] if last_target is None else [last_target],
            "observed_dead_target_guids": [],
            "last_observed_hostile_target_guid": last_target,
            "recent_direct_action_observations": recent,
        },
        "observed_start": {
            "phase": "START",
            "learning_role": "SERVER_OBSERVED_START_CONTROLLABLE_ACTION_PROXY",
            "action_key": "warrior.bloodthirst",
            "action_lane": "gcd",
            "ontology_status": "KNOWN_HISTORICAL_FURY_V3_CONTROLLABLE_ACTION",
            "server_observed_start_proxy": True,
            "client_action_request_observed": False,
            "client_next_swing_queue_intent_observed": False,
            "policy_decision_label": True,
            "order_key": [timestamp_ms, timestamp_ms, 0, 0],
            "anchor": {"timestamp_ms": timestamp_ms, "offset_ms": elapsed_ms},
            "spell": {"id": 23894, "name": "Bloodthirst"},
            "exact_target": {
                "guid": TARGET,
                "lane": "HOSTILE_CREATURE",
                "voting_enemy_target": True,
            },
        },
        "delay_observation_status": clone_v2.COMPLETED_DELAY,
        "prepared_training_weight": {
            "contract": prototypes_v1.WEIGHT_CONTRACT,
            "player_denominator": 1,
            "raid_denominator_within_player": 1,
            "recognized_start_denominator_within_player_raid": 2,
            "training_application_authorized": False,
            **_wire(Fraction(1, 2)),
        },
        "observation_boundaries": _boundaries(),
        "scientific_boundaries": {
            "training_authorized": False,
            "comparison_authorized": False,
        },
    }


def _tail() -> dict[str, object]:
    return {
        "schema": prototypes_v1.RECORD_SCHEMA,
        "implementation_revision": prototypes_v1.IMPLEMENTATION_REVISION,
        "record_type": "right_censored_wave_tail",
        "status": prototypes_v1.STATUS,
        "prototype_id": "fixture_prototype",
        "prototype_family": "PLAYER_CONDITIONED_STABLE_REPEAT_RAID",
        "source": _source(),
        "player_candidate_id": "player:fixture",
        "right_censored_tail": {
            "origin": "LAST_RECOGNIZED_CONTROLLABLE_START",
            "origin_timestamp_ms": 1_300,
            "window_end_timestamp_ms": 2_000,
            "duration_ms": 700,
            "preceding_recognized_controllable_start": {
                "order_key": [1_300, 1_300, 0, 0],
                "action_key": "warrior.bloodthirst",
                "action_lane": "gcd",
                "ontology_status": "KNOWN_HISTORICAL_FURY_V3_CONTROLLABLE_ACTION",
                "policy_decision_label": True,
            },
            "next_recognized_controllable_start_before_window_end": False,
            "later_unmapped_or_passive_starts_shorten_tail": False,
            "right_censored": True,
            "next_action_policy_label_observed": False,
        },
        "delay_observation_status": clone_v2.RIGHT_CENSORED_DELAY,
        "prepared_training_weight": None,
        "tail_weight_status": "UNASSIGNED_PENDING_CENSOR_AWARE_COMPILER",
        "observation_boundaries": _boundaries(),
        "scientific_boundaries": {
            "training_authorized": False,
            "comparison_authorized": False,
        },
    }


def _records() -> list[dict[str, object]]:
    first = _start(
        elapsed_ms=100,
        timestamp_ms=1_100,
        last_target=None,
        recent=[],
    )
    prior = {
        "order_key": [1_100, 1_100, 0, 0],
        "phase": "START",
        "action_key": "warrior.bloodthirst",
        "target_guid": TARGET,
    }
    second = _start(
        elapsed_ms=300,
        timestamp_ms=1_300,
        last_target=TARGET,
        recent=[prior],
    )
    return [first, second, _tail()]


def _canonical(value: object, *, newline: bool = False) -> bytes:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return payload + (b"\n" if newline else b"")


def _readdress(value: dict[str, object]) -> dict[str, object]:
    result = deepcopy(value)
    result.pop("content_address", None)
    result["content_address"] = {
        "algorithm": "sha256",
        "scope": "canonical JSON excluding content_address",
        "sha256": hashlib.sha256(_canonical(result)).hexdigest(),
    }
    return result


def _five_prototype_manifest(root: Path) -> Path:
    source_dir = root / "offline_data" / "derived" / "prototype_source"
    source_dir.mkdir(parents=True)
    prototypes = []
    for index in range(5):
        prototype_id = f"fixture_prototype_{index}"
        prototype = deepcopy(_prototype())
        prototype["prototype_id"] = prototype_id
        rows = deepcopy(_records())
        for row in rows:
            row["prototype_id"] = prototype_id
        logical = b"".join(_canonical(row, newline=True) for row in rows)
        partition_path = source_dir / f"{prototype_id}.jsonl.gz"
        with partition_path.open("wb") as raw:
            with gzip.GzipFile(
                filename="", mode="wb", fileobj=raw, mtime=0
            ) as compressed:
                compressed.write(logical)
        prototype["partition"] = {
            "path": partition_path.name,
            "record_schema": prototypes_v1.RECORD_SCHEMA,
            "record_count": len(rows),
            "prepared_start_observation_count": 2,
            "right_censored_tail_count": 1,
            "logical_size_bytes": len(logical),
            "logical_content_sha256": hashlib.sha256(logical).hexdigest(),
            "compressed_size_bytes": partition_path.stat().st_size,
            "compressed_file_sha256": hashlib.sha256(
                partition_path.read_bytes()
            ).hexdigest(),
        }
        prototypes.append(prototype)
    manifest = {
        "schema": prototypes_v1.MANIFEST_SCHEMA,
        "implementation_revision": prototypes_v1.IMPLEMENTATION_REVISION,
        "status": prototypes_v1.STATUS,
        "prototype_count_contract": {
            "new_materialized_prototype_count": 5,
            "stable_repeat_player_profile_count": 4,
            "player_equal_q4_pool_count": 1,
        },
        "prototypes": prototypes,
        "scientific_boundaries": {
            "training_authorized": False,
            "comparison_authorized": False,
            "same_equipment_matched_seed_comparison_authorized": False,
            "closed_loop_baseline_authorized": False,
            "deployment_authorized": False,
            "superiority_claim_authorized": False,
        },
    }
    manifest["content_address"] = {
        "algorithm": "sha256",
        "scope": "canonical JSON excluding content_address",
        "sha256": hashlib.sha256(_canonical(manifest)).hexdigest(),
    }
    path = source_dir / "manifest.json"
    path.write_bytes(_canonical(manifest, newline=True))
    return path


class HistoricalBehaviorCloneV2Tests(unittest.TestCase):
    def test_exact_fraction_heads_and_product_limit_censoring(self) -> None:
        model = clone_v2.compile_weighted_prototype_v2(
            _prototype(), _records(), source_binding=_binding()
        )

        self.assertEqual("fixture_prototype", model["prototype_identity"]["prototype_id"])
        self.assertEqual(
            Fraction(1, 1),
            _fraction(model["mark_head"]["global_counts"]["warrior.bloodthirst"]),
        )
        targets = model["target_head"]["by_action"]["warrior.bloodthirst"]
        self.assertEqual(Fraction(1, 2), _fraction(targets["OTHER_OR_NEW_ENEMY"]))
        self.assertEqual(Fraction(1, 2), _fraction(targets["CURRENT_ENEMY"]))

        delay = model["delay_head"]["global"]
        self.assertEqual(Fraction(2, 3), _fraction(delay["event_weight"]))
        self.assertEqual(Fraction(1, 3), _fraction(delay["censor_weight"]))
        buckets = {row["bucket"]: row for row in delay["buckets"]}
        self.assertEqual(Fraction(1, 1), _fraction(buckets["LE_100"]["at_risk_weight"]))
        self.assertEqual(Fraction(1, 3), _fraction(buckets["LE_100"]["event_weight"]))
        self.assertEqual(Fraction(2, 3), _fraction(buckets["LE_250"]["at_risk_weight"]))
        self.assertEqual(Fraction(1, 3), _fraction(buckets["LE_1000"]["censor_weight"]))
        self.assertEqual(
            Fraction(1, 3),
            _fraction(delay["residual_survival_after_last_bucket"]),
        )
        self.assertTrue(
            model["delay_head"]["usable_right_censored_tails_consumed"]
        )
        self.assertFalse(
            model["delay_head"]["terminal_tails_converted_to_pseudo_actions"]
        )
        self.assertEqual(2, model["compiler_summary"]["weighted_start_observation_count"])
        self.assertEqual(2, model["compiler_summary"]["completed_delay_observation_count"])
        self.assertEqual(1, model["compiler_summary"]["usable_right_censor_count"])
        self.assertEqual(0, model["compiler_summary"]["left_truncated_first_start_count"])
        self.assertEqual(0, model["compiler_summary"]["go_or_fail_policy_label_count"])
        self.assertTrue(
            all(
                row["unpaired_go_is_decision"] is False
                and row["v1_unpaired_go_semantics_reused"] is False
                and row["v2_policy_decision_phase"] == "RECOGNIZED_START_ONLY"
                for row in model["action_ontology"]
            )
        )
        self.assertFalse(model["claim_boundary"]["comparison_authorized"])
        self.assertFalse(model["claim_boundary"]["typed_simulator_adapter_complete"])
        clone_v2.validate_model_v2(json.loads(json.dumps(model)))

    def test_runtime_delay_projection_keeps_residual_survival_separate(self) -> None:
        model = clone_v2.compile_weighted_prototype_v2(
            _prototype(), _records(), source_binding=_binding()
        )
        # Cleave has no local delay cell in this fixture, so this selects the
        # global product-limit curve without conditional backoff.
        projected = clone_v2.predict_delay_distribution_v2(
            model, "warrior.cleave"
        )
        rows = {
            row["bucket"]: row
            for row in projected["buckets"]
            if _fraction(row["conditional_event_probability"]) > 0
        }

        self.assertEqual({"LE_100", "LE_250"}, set(rows))
        self.assertEqual(
            Fraction(1, 2),
            _fraction(rows["LE_100"]["conditional_event_probability"]),
        )
        self.assertEqual(
            Fraction(1, 2),
            _fraction(rows["LE_250"]["conditional_event_probability"]),
        )
        self.assertEqual(Fraction(1, 3), _fraction(projected["residual_survival_mass"]))
        self.assertTrue(projected["conditional_on_event_by_last_modeled_bucket"])
        self.assertFalse(
            projected["residual_survival_converted_to_action_or_finite_delay"]
        )
        self.assertTrue(projected["typed_adapter_required"])

    def test_left_truncated_first_start_is_mark_only_with_unknown_history(self) -> None:
        prototype = _prototype()
        support = prototype["observation_support"][0]["raids"][0]
        support["completed_delay_count"] = 1
        support["left_truncated_first_start_count"] = 1
        rows = _records()
        for row in rows:
            row["source"]["episode_window_coverage"] = {
                "partial": True,
                "left_unobserved_ms": 100,
                "right_unobserved_ms": 0,
                "wave_envelope_exactly_covers_metadata_window": False,
            }
        rows[0]["delay_observation_status"] = clone_v2.LEFT_TRUNCATED_DELAY

        model = clone_v2.compile_weighted_prototype_v2(
            prototype, rows, source_binding=_binding()
        )

        summary = model["compiler_summary"]
        self.assertEqual(2, summary["weighted_start_observation_count"])
        self.assertEqual(1, summary["completed_delay_observation_count"])
        self.assertEqual(1, summary["left_truncated_first_start_count"])
        self.assertEqual(
            Fraction(1, 2),
            _fraction(model["delay_head"]["global"]["event_weight"]),
        )
        self.assertNotIn(
            clone_v2.START_ACTION, model["delay_head"]["by_previous_action"]
        )
        context = model["mark_head"]["context_counts"]
        self.assertEqual(
            Fraction(1, 2),
            _fraction(
                context["sequence.last_controllable_action"]
                ["UNKNOWN_LEFT_TRUNCATED"]["warrior.bloodthirst"]
            ),
        )
        self.assertEqual(
            Fraction(1, 2),
            _fraction(
                context["coverage.prefix_observation_status"]["LEFT_TRUNCATED"]
                ["warrior.bloodthirst"]
            ),
        )

    def test_unusable_left_and_right_truncated_tail_is_retained_not_censored(self) -> None:
        prototype = _prototype()
        support = prototype["observation_support"][0]["raids"][0]
        support.update(
            {
                "recognized_start_count": 1,
                "wave_count": 2,
                "completed_delay_count": 1,
                "left_truncated_first_start_count": 0,
                "usable_right_censor_count": 1,
                "unusable_left_right_truncated_tail_count": 1,
            }
        )
        first = _records()[0]
        first["prepared_training_weight"].update(
            {
                "denominator": 1,
                "recognized_start_denominator_within_player_raid": 1,
                "value": 1.0,
            }
        )
        usable_tail = _tail()
        usable_tail["right_censored_tail"].update(
            {
                "origin_timestamp_ms": 1_100,
                "duration_ms": 900,
                "preceding_recognized_controllable_start": {
                    "order_key": [1_100, 1_100, 0, 0],
                    "action_key": "warrior.bloodthirst",
                    "action_lane": "gcd",
                    "ontology_status": "KNOWN_HISTORICAL_FURY_V3_CONTROLLABLE_ACTION",
                    "policy_decision_label": True,
                },
            }
        )
        unusable_tail = _tail()
        unusable_tail["source"].update({"wave_id": "wave-2", "wave_ordinal": 2})
        unusable_tail["right_censored_tail"].update(
            {
                "origin": "WAVE_FIRST_ANCHOR",
                "origin_timestamp_ms": 2_100,
                "window_end_timestamp_ms": 2_600,
                "duration_ms": 500,
                "preceding_recognized_controllable_start": None,
            }
        )
        unusable_tail["delay_observation_status"] = clone_v2.UNUSABLE_TRUNCATED_TAIL

        model = clone_v2.compile_weighted_prototype_v2(
            prototype,
            [first, usable_tail, unusable_tail],
            source_binding=_binding(),
        )

        summary = model["compiler_summary"]
        self.assertEqual(1, summary["usable_right_censor_count"])
        self.assertEqual(1, summary["unusable_left_right_truncated_tail_count"])
        self.assertEqual(
            Fraction(1, 2),
            _fraction(model["delay_head"]["global"]["censor_weight"]),
        )
        self.assertEqual(3, summary["source_record_count"])

    def test_source_binding_is_required_and_runtime_projection_is_float_only(self) -> None:
        with self.assertRaisesRegex(
            clone_v2.HistoricalBehaviorCloneV2Error, "source prototype_manifest"
        ):
            clone_v2.compile_weighted_prototype_v2(
                _prototype(), _records(), source_binding={}
            )
        bad_binding = _binding()
        bad_binding["prototype_manifest"]["path"] = "C:/location-dependent"
        with self.assertRaisesRegex(
            clone_v2.HistoricalBehaviorCloneV2Error, "location-dependent"
        ):
            clone_v2.compile_weighted_prototype_v2(
                _prototype(), _records(), source_binding=bad_binding
            )

        model = clone_v2.compile_weighted_prototype_v2(
            _prototype(), _records(), source_binding=_binding()
        )
        projection = model["runtime_bounded_float_projection"]
        self.assertTrue(
            projection[
                "exact_fraction_numerators_and_denominators_runtime_decode_forbidden"
            ]
        )
        self.assertIsInstance(
            projection["mark_global_weights"]["warrior.bloodthirst"], float
        )
        self.assertNotIn("path", model["source_binding"]["prototype_manifest"])

    def test_unknown_source_and_record_revisions_are_rejected(self) -> None:
        bad_record_rows = deepcopy(_records())
        bad_record_rows[0]["implementation_revision"] = "UNKNOWN"
        with self.assertRaisesRegex(
            clone_v2.HistoricalBehaviorCloneV2Error,
            "prototype record implementation_revision differs",
        ):
            clone_v2.compile_weighted_prototype_v2(
                _prototype(), bad_record_rows, source_binding=_binding()
            )

        for section in ("prototype_manifest", "prototype_partition"):
            with self.subTest(section=section):
                bad_binding = _binding()
                bad_binding[section]["implementation_revision"] = "UNKNOWN"
                with self.assertRaisesRegex(
                    clone_v2.HistoricalBehaviorCloneV2Error,
                    "implementation_revision differs",
                ):
                    clone_v2.compile_weighted_prototype_v2(
                        _prototype(), _records(), source_binding=bad_binding
                    )

    def test_readdressed_unknown_model_revision_is_rejected(self) -> None:
        model = clone_v2.compile_weighted_prototype_v2(
            _prototype(), _records(), source_binding=_binding()
        )
        model["implementation_revision"] = "UNKNOWN"
        model = _readdress(model)
        with self.assertRaisesRegex(
            clone_v2.HistoricalBehaviorCloneV2Error,
            "model implementation_revision differs",
        ):
            clone_v2.validate_model_v2(model)

    def test_readdressed_product_limit_hazard_tamper_is_rejected(self) -> None:
        model = clone_v2.compile_weighted_prototype_v2(
            _prototype(), _records(), source_binding=_binding()
        )
        exact_rows = model["delay_head"]["global"]["buckets"]
        index = next(
            index
            for index, row in enumerate(exact_rows)
            if _fraction(row["event_hazard"]) > 0
        )
        exact_rows[index]["event_hazard"] = _wire(Fraction())
        model["runtime_bounded_float_projection"]["delay_product_limit"][
            "global"
        ]["buckets"][index]["event_hazard"] = 0.0
        model = _readdress(model)
        with self.assertRaisesRegex(
            clone_v2.HistoricalBehaviorCloneV2Error,
            "event_hazard differs from product-limit identity",
        ):
            clone_v2.validate_model_v2(model)

    def test_readdressed_unknown_source_manifest_revision_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="clone_v2_source_revision_") as raw:
            root = Path(raw)
            prototype_manifest = _five_prototype_manifest(root)
            manifest = json.loads(prototype_manifest.read_text(encoding="utf-8"))
            manifest["implementation_revision"] = "UNKNOWN"
            manifest = _readdress(manifest)
            prototype_manifest.write_bytes(_canonical(manifest, newline=True))
            with self.assertRaisesRegex(
                clone_v2.HistoricalBehaviorCloneV2Error,
                "prototype manifest implementation_revision differs",
            ):
                clone_v2.build_historical_behavior_clones_v2(
                    prototype_manifest_path=prototype_manifest,
                    output_directory=(
                        root / "offline_data" / "derived" / "clone_v2"
                    ),
                )

    def test_missing_terminal_tail_is_rejected_not_ignored(self) -> None:
        with self.assertRaisesRegex(
            clone_v2.HistoricalBehaviorCloneV2Error,
            "without an explicit wave censor tail",
        ):
            clone_v2.compile_weighted_prototype_v2(
                _prototype(), _records()[:-1], source_binding=_binding()
            )

    def test_build_materializes_five_distinct_bound_models(self) -> None:
        with tempfile.TemporaryDirectory(prefix="clone_v2_build_") as raw:
            root = Path(raw)
            prototype_manifest = _five_prototype_manifest(root)
            clone_output = root / "offline_data" / "derived" / "clone_v2"
            result = clone_v2.build_historical_behavior_clones_v2(
                prototype_manifest_path=prototype_manifest,
                output_directory=clone_output,
            )
            manifest = json.loads(result.manifest.read_text(encoding="utf-8"))

            self.assertEqual(5, result.model_count)
            self.assertEqual(10, result.weighted_start_observation_count)
            self.assertEqual(10, result.completed_delay_observation_count)
            self.assertEqual(5, result.usable_right_censor_count)
            self.assertEqual(0, result.left_truncated_first_start_count)
            self.assertEqual(0, result.unusable_left_right_truncated_tail_count)
            self.assertEqual(5, len({row["prototype_id"] for row in manifest["models"]}))
            self.assertEqual(5, len({row["path"] for row in manifest["models"]}))
            self.assertTrue(
                manifest["input_closure"][
                    "prototype_partitions_streamed_one_at_a_time"
                ]
            )
            self.assertFalse(
                manifest["scientific_boundaries"]["comparison_authorized"]
            )
            self.assertNotIn(
                "path", manifest["input_closure"]["prototype_manifest"]
            )
            for row in manifest["models"]:
                model = json.loads(
                    (result.manifest.parent / row["path"]).read_text(encoding="utf-8")
                )
                self.assertEqual(
                    row["prototype_id"], model["prototype_identity"]["prototype_id"]
                )
                self.assertEqual(
                    row["prototype_id"], model["source_binding"]["prototype_id"]
                )
                self.assertTrue(
                    model["delay_head"]["usable_right_censored_tails_consumed"]
                )
                self.assertEqual(
                    prototypes_v1.QUEUE_INTENT_STATUS,
                    model["uncertainty_contract"][
                        "client_next_swing_queue_intent"
                    ],
                )
                self.assertFalse(model["claim_boundary"]["full_rollout_complete"])

            second_root = root / "same_bytes_different_location"
            second_manifest = _five_prototype_manifest(second_root)
            second_result = clone_v2.build_historical_behavior_clones_v2(
                prototype_manifest_path=second_manifest,
                output_directory=(
                    second_root / "offline_data" / "derived" / "clone_v2"
                ),
            )
            second = json.loads(
                second_result.manifest.read_text(encoding="utf-8")
            )
            self.assertEqual(
                [row["content_sha256"] for row in manifest["models"]],
                [row["content_sha256"] for row in second["models"]],
            )
            self.assertEqual(
                manifest["content_address"]["sha256"],
                second["content_address"]["sha256"],
            )


if __name__ == "__main__":
    unittest.main()
