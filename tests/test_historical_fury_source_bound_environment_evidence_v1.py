from __future__ import annotations

from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps import historical_fury_source_bound_environment_evidence_v1 as evidence


FOCAL = "0x0000000000000001"
TEAMMATE = "0x0000000000000002"
T1 = "0xF130000101000001"
T2 = "0xF130000102000002"
T3 = "0xF130000103000003"


def _anchor(timestamp: int, index: int, *, event_type: str = "DMG") -> dict[str, object]:
    stream_types = {
        "DMG": "damage",
        "HEAL": "heal",
        "DEAD": "slain",
        "START": "spell_start",
        "GO": "spell_go",
        "FAIL": "spell_fail",
        "CLASS": "unit_classification",
    }
    return {
        "timestamp_ms": timestamp,
        "offset_ms": timestamp,
        "event_index": index,
        "stream_type": stream_types[event_type.upper()],
        "frame_index": 0,
        "frame_message_index": index,
        "official_message_sha256": f"{index + 1:064x}",
    }


def _request() -> dict[str, object]:
    return {
        "segment_ref": "sha256:" + "1" * 64,
        "source_identity": {
            "instance_id": "instance-0",
            "encounter_id": "encounter-0",
            "wave_id": "wave-0",
            "wave_ordinal": 1,
            "player_guid": FOCAL,
            "source_wave_content_sha256": "2" * 64,
        },
        "decision_prefix": {
            "decision_count": 2,
            "first_decision": {
                "timestamp_ms": 10_000,
                "event_index": 10,
                "order_key": [10_000, 10, 3, 10],
                "action_key": "warrior.bloodthirst",
            },
            "last_bound_decision": {
                "timestamp_ms": 35_000,
                "event_index": 35,
                "order_key": [35_000, 35, 3, 35],
                "action_key": "warrior.execute",
            },
        },
    }


def _transition(
    timestamp: int,
    index: int,
    action: str,
    *,
    seen: list[str],
    alive: list[str],
    dead: list[str],
    damage: dict[str, int],
) -> dict[str, object]:
    return {
        "trace_index": index,
        "feature_cutoff_is_strict_prefix": True,
        "current_event_present_in_state_before": False,
        "future_outcomes_in_state_before": False,
        "observed_event": {
            "policy_decision_label": True,
            "source_guid": FOCAL,
            "action_key": action,
            "action_lane": "gcd",
            "order_key": [timestamp, index, 3, index],
            "anchor": _anchor(timestamp, index, event_type="START"),
            "exact_target": {
                "guid": alive[0] if alive else None,
                "voting_enemy_target": bool(alive),
            },
        },
        "state_before": {
            "seen_hostile_target_guids": seen,
            "alive_seen_hostile_target_guids": alive,
            "observed_dead_target_guids": dead,
            "last_observed_hostile_target_guid": alive[0] if alive else None,
            "prefix_direct_damage_amount": sum(damage.values()),
            "prefix_direct_damage_by_target": [
                {"target_guid": guid, "damage_amount": amount}
                for guid, amount in sorted(damage.items())
            ],
        },
    }


def _episode() -> dict[str, object]:
    return {
        "wave_observations": [
            {
                "wave_id": "wave-0",
                "wave_ordinal": 1,
                "source_wave_content_sha256": "2" * 64,
                "prefix_transitions": [
                    _transition(
                        10_000,
                        10,
                        "warrior.bloodthirst",
                        seen=[T1],
                        alive=[T1],
                        dead=[],
                        damage={T1: 100},
                    ),
                    _transition(
                        20_000,
                        20,
                        "ignored.outside.boundary",
                        seen=[T1],
                        alive=[T1],
                        dead=[],
                        damage={T1: 120},
                    ),
                    _transition(
                        35_000,
                        35,
                        "warrior.execute",
                        seen=[T1, T2, T3],
                        alive=[T2, T3],
                        dead=[T1],
                        damage={T1: 150, T2: 20},
                    ),
                ],
            }
        ]
    }


def _target(guid: str, amount: int, *, dead: bool) -> dict[str, object]:
    return evidence._target_projection(
        {
            "voting_for_simulator_target_model": True,
            "target_guid": guid,
            "first_activity_anchor": _anchor(9000, 1),
            "last_activity_anchor": _anchor(39_000, 39),
            "damage_received": {
                "amount_source": "DMG_ONLY",
                "amount": amount,
                "event_count": 1,
                "unattributed_amount": 0,
                "by_exact_player": [],
                "through_first_death": {
                    "amount": amount,
                    "event_count": 1,
                    "interpretation": "observed DMG sum, not exact maximum or initial health",
                },
            },
            "healing_received": {"amount": 0},
            "death": {
                "observed": dead,
                "anchor": _anchor(15_000, 15, event_type="DEAD") if dead else None,
                "damage_amount_added_from_slain": 0,
            },
            "armor": {
                "status": "MISSING_NOT_IN_EXTERNAL_CORE_STREAM_SET",
                "effective_armor": None,
                "aura_transitions": None,
            },
        }
    )


def _event(
    timestamp: int,
    index: int,
    target: str,
    *,
    actor: str | None,
    amount: int | None = None,
    attribution_kind: str = "DIRECT_FRIENDLY_PLAYER",
    source_guid: str | None = None,
    event_type: str = "DMG",
    spell_id: int | None = None,
    spell_name: str | None = None,
    voting: bool = True,
) -> dict[str, object]:
    event: dict[str, object] = {
        "event_type": event_type,
        "anchor": _anchor(timestamp, index, event_type=event_type),
        "source": {"guid": source_guid or actor},
        "target": {"guid": target, "voting_enemy_target": voting},
        "spell": {"id": spell_id, "name": spell_name},
        "attribution": {
            "player_guid": actor,
            "attribution_kind": attribution_kind,
        },
    }
    if event_type == "DMG":
        event["damage"] = {"amount": amount, "amount_source": "DMG_ONLY"}
    return event


class SourceBoundEnvironmentEvidenceV1Tests(unittest.TestCase):
    def test_decision_prefixes_are_delta_encoded_without_future_backfill(self) -> None:
        request = _request()
        request["decision_prefix"]["decision_count"] = 3
        rows, summary = evidence._decision_evidence(request=request, episode=_episode())

        self.assertEqual(3, summary["decision_count"])
        self.assertEqual(25_000, summary["span_ms"])
        self.assertEqual([T1], rows[0]["strict_prefix_state"]["alive_target_guids"])
        self.assertEqual([T2, T3], rows[-1]["strict_prefix_state"]["alive_target_guids"])
        self.assertEqual(
            [T2, T3], rows[-1]["strict_prefix_state"]["new_seen_target_guids"]
        )
        self.assertEqual([T1], rows[-1]["strict_prefix_state"]["new_dead_target_guids"])
        self.assertFalse(rows[-1]["strict_prefix_state"]["future_target_backfill_used"])
        self.assertIn("segment_entry_snapshot", rows[0]["strict_prefix_state"])
        self.assertNotIn("segment_entry_snapshot", rows[1]["strict_prefix_state"])

    def test_windows_and_focal_loo_use_attributed_player_guid(self) -> None:
        request = _request()
        decisions, _ = evidence._decision_evidence(
            request={**request, "decision_prefix": {**request["decision_prefix"], "decision_count": 3}},
            episode=_episode(),
        )
        request["_decision_rows"] = decisions

        focal_events = [
            _event(9_998, 8, T1, actor=FOCAL, amount=3, voting=False),
            _event(10_000, 11, T1, actor=FOCAL, amount=100),
            _event(
                10_001,
                12,
                T1,
                actor=FOCAL,
                amount=50,
                attribution_kind="EXACT_OFFICIAL_OWNER",
                source_guid="0xF140000001000001",
            ),
            _event(
                10_005,
                15,
                T1,
                actor=FOCAL,
                event_type="GO",
                spell_id=11597,
                spell_name="Sunder Armor",
            ),
        ]
        teammate_events = [
            _event(
                9_997,
                7,
                T1,
                actor=TEAMMATE,
                event_type="GO",
                spell_id=11597,
                spell_name="Sunder Armor",
            ),
            _event(9_999, 9, T1, actor=TEAMMATE, amount=5),
            _event(10_002, 12, T1, actor=TEAMMATE, amount=30),
            _event(
                10_003,
                13,
                T2,
                actor=TEAMMATE,
                amount=20,
                attribution_kind="EXACT_OFFICIAL_CONTROLLER",
            ),
            _event(30_001, 30, T1, actor=TEAMMATE, amount=7),
            _event(30_002, 31, T1, actor=TEAMMATE, amount=11),
            _event(35_000, 35, T3, actor=TEAMMATE, amount=13),
            _event(35_000, 36, T3, actor=TEAMMATE, amount=17),
            _event(
                35_000,
                37,
                T1,
                actor=TEAMMATE,
                event_type="GO",
                spell_id=11597,
                spell_name="Sunder Armor",
            ),
        ]
        unattributed = _event(
            10_004,
            14,
            T3,
            actor=None,
            amount=999,
            attribution_kind="UNATTRIBUTED",
        )
        target_amounts = {
            T1: 3 + 5 + 100 + 50 + 30 + 7 + 11,
            T2: 20,
            T3: 13 + 17 + 999,
        }
        targets = [
            _target(T1, target_amounts[T1], dead=True),
            _target(T2, target_amounts[T2], dead=False),
            _target(T3, target_amounts[T3], dead=False),
        ]
        for target_index, target in enumerate(targets):
            target["target_index"] = target_index
        record = {
            "reconstruction_binding": {
                "window": {
                    "first_anchor": _anchor(0, 0),
                    "last_context_anchor": _anchor(40_000, 40),
                }
            },
            "players": [
                {"player": {"guid": FOCAL}, "timeline": focal_events},
                {"player": {"guid": TEAMMATE}, "timeline": teammate_events},
            ],
            "unattributed_lane": {"timeline": [unattributed]},
            "classification_transitions_inside_window": [
                {
                    "guid": T1,
                    "lane": "HOSTILE_CREATURE",
                    "unit_type": {"numeric": 3, "label": "CREATURE"},
                    "affiliation": {"numeric": 16, "label": "HOSTILE"},
                    "resolution_status": "OFFICIAL_CLASSIFICATION",
                    "spell_id": 0,
                    "anchor": _anchor(10_000, 9, event_type="CLASS"),
                }
            ],
        }
        result = evidence._timeline_evidence(
            request=request,
            record=record,
            targets=targets,
            diagnostic_duration_ms=20_001,
        )

        windows = result["windows"]
        self.assertEqual(30_001, windows["base_request_diagnostic_slice"]["end_timestamp_ms"])
        self.assertEqual(35_000, windows["bound_decision_support"]["end_timestamp_ms"])
        diagnostic = result["team_trace"]["diagnostic_slice_accounting"]
        union = result["team_trace"]["materialized_union_accounting"]
        self.assertEqual(57, diagnostic["loo_damage"])
        self.assertEqual(150, diagnostic["excluded_focal_damage"])
        self.assertEqual(81, union["loo_damage"])
        self.assertEqual(150, union["excluded_focal_damage"])
        self.assertEqual(999, union["unattributed_damage_not_entering_runtime_schedule"])
        self.assertIn(
            T3,
            result["target_registry_evidence"][
                "diagnostic_slice_event_time_voting_damage_target_guids"
            ],
        )
        self.assertTrue(
            all(
                event["actor_player_guid"] != FOCAL
                for event in result["team_trace"]["leave_one_out_exact_player_damage_events"]
            )
        )
        armor = result["armor_cast_candidate_evidence"]
        self.assertEqual(3, len(armor["full_reconstruction_wave_retrospective"]))
        self.assertEqual(1, len(armor["materialized_team_trace_union"]))
        self.assertEqual(2, armor["outside_materialized_union_count"])
        self.assertEqual(
            "CANDIDATE_ENDOGENOUS",
            armor["materialized_team_trace_union"][0]["actor_role"],
        )
        self.assertFalse(
            armor["materialized_team_trace_union"][0]["aura_apply_or_stack_proven"]
        )
        self.assertEqual(1, len(result["classification_transition_evidence"]))
        lifecycle = targets[0]["target_lifecycle_evidence"]
        self.assertEqual(
            1,
            lifecycle["event_time_nonvoting_positive_damage"]["event_count"],
        )
        hypotheses = evidence._pack_hypotheses(
            targets,
            target_guids={T1, T2, T3},
            registry_scope="FIXTURE",
            lifecycle_anchor_mode="RETROSPECTIVE_FINAL_TARGET_IDENTITY",
        )
        self.assertTrue(
            all(
                component["end_timestamp_ms"] >= component["start_timestamp_ms"]
                for hypothesis in hypotheses
                for component in hypothesis["components"]
            )
        )
        prefix_hypotheses = evidence._pack_hypotheses(
            targets,
            target_guids={T1, T2, T3},
            registry_scope="SEEN_BY_LAST_BOUND_DECISION_STRICT_PREFIX",
            lifecycle_anchor_mode=(
                "STRICT_PREFIX_EVENT_TIME_VOTING_CENSORED_AT_LAST_BOUND_DECISION"
            ),
        )
        self.assertTrue(all(not hypothesis["future_suffix_used"] for hypothesis in prefix_hypotheses))
        self.assertTrue(
            all(
                component["end_timestamp_ms"] <= 35_000
                for hypothesis in prefix_hypotheses
                for component in hypothesis["components"]
            )
        )
        self.assertTrue(
            any(
                T2 in component["right_censored_target_guids"]
                for hypothesis in prefix_hypotheses
                for component in hypothesis["components"]
            )
        )
        self.assertIsNone(targets[0]["health_evidence"]["exact_initial_or_max_health"])
        self.assertIsNone(targets[0]["armor_evidence"]["exact_effective_armor_events"])
        self.assertIsNone(targets[0]["attackability_evidence"]["exact_intervals"])

    def test_stream_selection_verifies_the_unselected_tail_at_eof(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "rows.jsonl.gz"
            rows = [{"id": "wanted", "value": 1}, {"id": "tail", "value": 2}]
            logical = b"".join(evidence._canonical_bytes(row, newline=True) for row in rows)
            compressed = evidence._gzip_bytes(logical)
            path.write_bytes(compressed)
            descriptor = {
                "instance_id": "instance-0",
                "record_schema": "fixture/v1",
                "record_count": 2,
                "logical_size_bytes": len(logical),
                "logical_content_sha256": hashlib.sha256(logical).hexdigest(),
                "compressed_size_bytes": len(compressed),
                "compressed_file_sha256": hashlib.sha256(compressed).hexdigest(),
            }
            selected, closure = evidence._stream_selected_jsonl(
                path=path,
                descriptor=descriptor,
                needles=[b"wanted"],
                selector=lambda row: row.get("id") == "wanted",
                label="fixture",
            )
            self.assertEqual([rows[0]], selected)
            self.assertEqual(1, closure["decoded_candidate_row_count"])
            self.assertTrue(closure["full_logical_partition_verified_at_eof"])

            broken = deepcopy(descriptor)
            broken["record_count"] = 1
            with self.assertRaisesRegex(
                evidence.HistoricalFurySourceBoundEnvironmentEvidenceV1Error,
                "record_count differs",
            ):
                evidence._stream_selected_jsonl(
                    path=path,
                    descriptor=broken,
                    needles=[b"wanted"],
                    selector=lambda row: row.get("id") == "wanted",
                    label="fixture",
                )

    def test_reconstruction_loader_keeps_multiple_requested_waves_per_encounter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            waves = [
                evidence._content_addressed({"wave_id": "wave-1", "targets": []}),
                evidence._content_addressed({"wave_id": "wave-2", "targets": []}),
            ]
            artifact = evidence._content_addressed(
                {
                    "schema": evidence.reconstruction_v2.ARTIFACT_SCHEMA,
                    "implementation_revision": evidence.reconstruction_v2.IMPLEMENTATION_REVISION,
                    "status": evidence.reconstruction_v2.STATUS,
                    "instance_id": "instance-0",
                    "encounter_id": "encounter-0",
                    "summary": {"wave_count": 2},
                    "waves": waves,
                }
            )
            logical = evidence._canonical_bytes(artifact)
            compressed = evidence._gzip_bytes(logical)
            artifact_path = directory / "encounter.json.gz"
            artifact_path.write_bytes(compressed)
            manifest = {
                "instances": [
                    {
                        "instance_id": "instance-0",
                        "encounters": [
                            {
                                "encounter_id": "encounter-0",
                                "summary": artifact["summary"],
                                "artifact": {
                                    "path": artifact_path.name,
                                    "compressed_size_bytes": len(compressed),
                                    "compressed_file_sha256": hashlib.sha256(compressed).hexdigest(),
                                    "logical_size_bytes": len(logical),
                                    "content_sha256": artifact["content_address"]["sha256"],
                                },
                            }
                        ],
                    }
                ]
            }
            requests = [
                {
                    "source_identity": {
                        "instance_id": "instance-0",
                        "encounter_id": "encounter-0",
                        "wave_id": wave_id,
                    }
                }
                for wave_id in ("wave-1", "wave-2")
            ]

            selected, closure = evidence._load_selected_reconstruction(
                manifest=manifest,
                manifest_path=directory / "manifest.json",
                request_rows=requests,
            )

            self.assertEqual(
                {
                    ("instance-0", "encounter-0", "wave-1"),
                    ("instance-0", "encounter-0", "wave-2"),
                },
                set(selected),
            )
            self.assertEqual(1, len(closure))
            self.assertEqual(2, closure[0]["selected_wave_count"])

    def test_leave_instance_out_health_prior_excludes_healed_deaths(self) -> None:
        def row(instance: str, wave: str, guid: str, proxy: int | None, healing: int) -> dict[str, object]:
            return {
                "source_identity": {"instance_id": instance, "wave_id": wave},
                "targets": [
                    {
                        "target_guid": guid,
                        "creature_entry_id": 321,
                        "historical_outcome": {"observed_healing_received": healing},
                        "health_evidence": {
                            "retrospective_kill_budget_proxy": proxy,
                            "same_entry_leave_instance_out_prior": None,
                        },
                    }
                ],
            }

        rows = [
            row("instance-a", "wave-a", "target-a", 100, 0),
            row("instance-b", "wave-b", "target-b", 250, 50),
            row("instance-c", "wave-c", "target-c", None, 0),
        ]
        evidence._attach_leave_instance_out_priors(rows)

        prior = rows[2]["targets"][0]["health_evidence"][
            "same_entry_leave_instance_out_prior"
        ]
        self.assertEqual(1, prior["support_count"])
        self.assertEqual(100, prior["median"])
        self.assertEqual(1, prior["excluded_healed_death_support_count"])
        self.assertEqual(
            "DEAD_TARGET_WITH_ZERO_OBSERVED_HEALING", prior["support_filter"]
        )

    def test_validator_rejects_promoted_health_or_focal_loo_leakage(self) -> None:
        row = evidence._content_addressed(
            {
                "schema": evidence.ROW_SCHEMA,
                "implementation_revision": evidence.IMPLEMENTATION_REVISION,
                "status": evidence.ROW_STATUS,
                "derived_request_template": None,
                "dynamic_load_config": None,
                "execution_window_contract": {"selected_execution_window": None},
                "identifiability": {
                    "exact_initial_or_max_health": False,
                    "exact_base_or_effective_armor": False,
                    "exact_attackability_intervals": False,
                    "counterfactual_team_kill_clock": False,
                    "external_core_aura_events_available": False,
                },
                "blockers": [{"code": code} for code in sorted(evidence._REQUIRED_BLOCKERS)],
                "team_trace": {
                    "focal_player_guid": FOCAL,
                    "leave_one_out_exact_player_damage_events": [
                        {
                            "actor_player_guid": TEAMMATE,
                            "window_membership": {
                                "materialized_team_trace_union": True
                            },
                        }
                    ],
                    "excluded_focal_exact_player_damage_events": [
                        {
                            "actor_player_guid": FOCAL,
                            "window_membership": {
                                "materialized_team_trace_union": True
                            },
                        }
                    ],
                },
                "armor_cast_candidate_evidence": {
                    "materialized_team_trace_union": [],
                    "full_reconstruction_wave_retrospective": [],
                    "outside_materialized_union_count": 0,
                },
                "pack_hypothesis_family": [],
                "targets": [
                    {
                        "target_index": 0,
                        "health_evidence": {
                            "exact_initial_or_max_health": None,
                            "same_entry_leave_instance_out_prior": {
                                "support_filter": "DEAD_TARGET_WITH_ZERO_OBSERVED_HEALING"
                            },
                        },
                        "armor_evidence": {
                            "exact_base_armor": None,
                            "exact_effective_armor_events": None,
                        },
                        "attackability_evidence": {"exact_intervals": None},
                        "target_lifecycle_evidence": {
                            "event_time_runtime_lane_backfilled": False
                        },
                    }
                ],
            }
        )
        evidence._validate_row(row)

        promoted = deepcopy(row)
        promoted["targets"][0]["health_evidence"]["exact_initial_or_max_health"] = 10
        promoted = evidence._content_addressed(promoted)
        with self.assertRaisesRegex(
            evidence.HistoricalFurySourceBoundEnvironmentEvidenceV1Error,
            "promotes an unobserved exact state",
        ):
            evidence._validate_row(promoted)

        leaked = deepcopy(row)
        leaked["team_trace"]["leave_one_out_exact_player_damage_events"][0][
            "actor_player_guid"
        ] = FOCAL
        leaked = evidence._content_addressed(leaked)
        with self.assertRaisesRegex(
            evidence.HistoricalFurySourceBoundEnvironmentEvidenceV1Error,
            "survived leave-one-player-out",
        ):
            evidence._validate_row(leaked)


if __name__ == "__main__":
    unittest.main()
