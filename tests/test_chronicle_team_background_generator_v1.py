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

from o2o_dps.chronicle_team_background_generator_v1 import (
    ChronicleTeamBackgroundGeneratorError,
    IMPLEMENTATION_REVISION,
    NO_RETARGET_CONTROL,
    SCHEMA,
    build_chronicle_team_background_generator,
    draw_background_schedule,
    iter_runtime_schedule,
    replay_background_schedule,
    validate_background_draw,
    validate_background_generator_manifest,
    write_background_draw,
)
from o2o_dps.chronicle_team_wave_model_v1 import (
    IMPLEMENTATION_REVISION as TEAM_MODEL_IMPLEMENTATION_REVISION,
)


TEAM_SCHEMA = "chronicle_team_wave_model/v1"
INSTANCE_A = "11111111-1111-4111-8111-111111111111"
INSTANCE_B = "22222222-2222-4222-8222-222222222222"
FOCAL_A = "0x00000000000000A1"
FOCAL_B = "0x00000000000000B1"
ARMS = "0x00000000000000A2"
MAGE = "0x00000000000000A3"
FOCAL_PET = "0xF140001111000001"
TARGET_A = "0xF13000AAAA000001"
TARGET_B = "0xF13000BBBB000002"
NONHOSTILE_ACTION_TARGET = "0x00000000000000FF"
COMPONENT_A = "component-a"
COMPONENT_B = "component-b"


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


def _wave(instance: str, ordinal: int) -> dict[str, object]:
    encounter = f"{instance}-encounter-{ordinal}"
    return {
        "instance_id": instance,
        "encounter_id": encounter,
        "wave_id": f"{encounter}:wave:1",
        "wave_ordinal": ordinal,
    }


def _membership(instance_node: str, player_node: str | None) -> dict[str, object]:
    return {
        "instance_node_id": instance_node,
        "guild_node_ids": [],
        "player_node_id": player_node,
        "component_edges": [],
        "required_split_unit": "connected component of instance, guild, and player nodes",
        "row_random_split_allowed": False,
    }


def _episode(
    wave: dict[str, object],
    *,
    guid: str,
    name: str,
    hero_class: str,
    partition: str,
    instance_node: str,
    player_node: str,
) -> dict[str, object]:
    return {
        "schema": TEAM_SCHEMA,
        "record_type": "player_wave_episode",
        "wave": wave,
        "episode_id": hashlib.sha256(
            f"{wave['wave_id']}|{guid.casefold()}".encode("utf-8")
        ).hexdigest(),
        "player": {
            "guid": guid,
            "name": name,
            "hero_class": hero_class,
            "specialization": {
                "status": "OBSERVED_SINGLE",
                "observed_values": [partition],
                "partition_key": partition,
                "fury_arms_merged": False,
                "inference_from_talents_used": False,
            },
        },
        "component_membership": _membership(instance_node, player_node),
        "eligibility": {
            "team_behavior_training_eligible": True,
            "historical_fury_policy_training_eligible": partition == "WARRIOR_FURY",
        },
        "prefix_transitions": [],
    }


def _event(
    wave: dict[str, object],
    index: int,
    relative_ms: int,
    event_type: str,
    *,
    source_guid: str,
    target_guid: str,
    attribution_kind: str,
    owner_guid: str | None,
    amount: int | None = None,
    spell: str = "Attack",
) -> dict[str, object]:
    absolute = 1000 + relative_ms
    return {
        "schema": "chronicle_team_wave_timeline/v1",
        "record_type": "event",
        "wave": wave,
        "order_key": [absolute, index, 100 + index],
        "offset_ms": absolute,
        "wave_offset_ms": relative_ms,
        "event_index": index,
        "csv_line": 100 + index,
        "event_type": event_type,
        "source_event_type": "GO" if event_type == "CAST" else event_type,
        "source": {"guid": source_guid, "name": source_guid},
        "target": {"guid": target_guid, "name": target_guid},
        "spell": {"id": index, "name": spell},
        "amount": amount,
        "amount_status": (
            "PARSED_NONNEGATIVE_NUMERIC"
            if amount is not None
            else "NOT_APPLICABLE"
        ),
        "outcome": None,
        "attribution": {
            "kind": attribution_kind,
            "player_guid": owner_guid,
            "evidence": "SYNTHETIC_EXPLICIT",
        },
    }


def _wave_rows(
    *,
    instance: str,
    ordinal: int,
    contamination: str,
    focal: str,
    instance_node: str,
    focal_node: str,
    include_team: bool,
) -> list[dict[str, object]]:
    wave = _wave(instance, ordinal)
    rows: list[dict[str, object]] = [
        {
            "schema": TEAM_SCHEMA,
            "record_type": "wave_model_header",
            "wave": wave,
            "scenario": {"scenario_id": f"scenario-{instance}-{ordinal}"},
            "contamination": {
                "status": contamination,
                "guild_name": "南北" if contamination == "SUSPECT_36YD_RANGE_BUG" else "CleanGuild",
            },
            "training_eligibility": {
                "default_eligible": contamination
                in ("POSTFIX_KNOWN_CLEAN", "NO_KNOWN_RULE_MATCH"),
                "eligible_statuses": ["NO_KNOWN_RULE_MATCH", "POSTFIX_KNOWN_CLEAN"],
                "nonvoting_statuses": ["SUSPECT_36YD_RANGE_BUG", "UNKNOWN_NONVOTING"],
            },
            "lane_contract": {
                "exact_trace_status": "DESCRIPTIVE_NONVOTING",
                "exact_trace_can_vote": False,
                "comparison_ready": False,
            },
            "source_hashes": {"fixture": "a" * 64},
        }
    ]
    exact: list[dict[str, object]] = [
        _event(
            wave,
            1,
            1,
            "DMG",
            source_guid=focal,
            target_guid=TARGET_A,
            attribution_kind="DIRECT_PLAYER",
            owner_guid=focal,
            amount=30,
            spell="Focal direct",
        ),
        _event(
            wave,
            2,
            2,
            "DMG",
            source_guid=FOCAL_PET,
            target_guid=TARGET_A,
            attribution_kind="OWNED_ENTITY",
            owner_guid=focal,
            amount=20,
            spell="Focal owned",
        ),
    ]
    if include_team:
        exact.extend(
            [
                _event(
                    wave,
                    3,
                    5,
                    "START",
                    source_guid=ARMS,
                    target_guid=TARGET_A,
                    attribution_kind="DIRECT_PLAYER",
                    owner_guid=ARMS,
                    spell="Mortal Strike",
                ),
                _event(
                    wave,
                    4,
                    10,
                    "DMG",
                    source_guid=ARMS,
                    target_guid=TARGET_A,
                    attribution_kind="DIRECT_PLAYER",
                    owner_guid=ARMS,
                    amount=60,
                    spell="Mortal Strike",
                ),
                _event(
                    wave,
                    5,
                    11,
                    "DMG",
                    source_guid="0xF13000FFFF000001",
                    target_guid=TARGET_A,
                    attribution_kind="UNATTRIBUTED",
                    owner_guid=None,
                    amount=10,
                    spell="Unknown periodic",
                ),
                _event(
                    wave,
                    6,
                    12,
                    "DMG",
                    source_guid=ARMS,
                    target_guid=TARGET_B,
                    attribution_kind="DIRECT_PLAYER",
                    owner_guid=ARMS,
                    amount=40,
                    spell="Cleave",
                ),
                _event(
                    wave,
                    7,
                    13,
                    "DMG",
                    source_guid=MAGE,
                    target_guid=TARGET_A,
                    attribution_kind="DIRECT_PLAYER",
                    owner_guid=MAGE,
                    amount=15,
                    spell="Fireball",
                ),
                _event(
                    wave,
                    8,
                    14,
                    "DMG",
                    source_guid=MAGE,
                    target_guid=TARGET_B.lower(),
                    attribution_kind="DIRECT_PLAYER",
                    owner_guid=MAGE,
                    amount=20,
                    spell="Ignite",
                ),
                _event(
                    wave,
                    9,
                    30,
                    "DMG",
                    source_guid=ARMS,
                    target_guid=TARGET_A,
                    attribution_kind="DIRECT_PLAYER",
                    owner_guid=ARMS,
                    amount=50,
                    spell="Future hit",
                ),
                _event(
                    wave,
                    10,
                    31,
                    "DEAD",
                    source_guid=ARMS,
                    target_guid=TARGET_A,
                    attribution_kind="DIRECT_PLAYER",
                    owner_guid=ARMS,
                    amount=999,
                    spell="Historical DEAD value",
                ),
                _event(
                    wave,
                    11,
                    45,
                    "CAST",
                    source_guid="0xF13000FFFF000001",
                    target_guid=NONHOSTILE_ACTION_TARGET,
                    attribution_kind="UNATTRIBUTED",
                    owner_guid=None,
                    spell="Late unknown action",
                ),
            ]
        )
    rows.extend(
        {
            "schema": TEAM_SCHEMA,
            "record_type": "exact_trace_event",
            "wave": wave,
            "lane": {"status": "DESCRIPTIVE_NONVOTING", "voting_eligible": False},
            "event": event,
        }
        for event in exact
    )
    rows.append(
        _episode(
            wave,
            guid=focal,
            name="Focal",
            hero_class="WARRIOR",
            partition="WARRIOR_FURY",
            instance_node=instance_node,
            player_node=focal_node,
        )
    )
    if include_team:
        rows.extend(
            [
                _episode(
                    wave,
                    guid=ARMS,
                    name="Arms teammate",
                    hero_class="WARRIOR",
                    partition="WARRIOR_ARMS",
                    instance_node=instance_node,
                    player_node="player-arms",
                ),
                _episode(
                    wave,
                    guid=MAGE,
                    name="Mage teammate",
                    hero_class="MAGE",
                    partition="MAGE_FIRE",
                    instance_node=instance_node,
                    player_node="player-mage",
                ),
            ]
        )
    rows.append(
        {
            "schema": TEAM_SCHEMA,
            "record_type": "unattributed_wave_episode",
            "wave": wave,
            "actor": {"kind": "UNATTRIBUTED_UNKNOWN"},
            "component_membership": _membership(instance_node, None),
            "eligibility": {"team_behavior_training_eligible": False},
            "prefix_transitions": [],
        }
    )
    for target in (TARGET_A, TARGET_B):
        rows.append(
            {
                "schema": TEAM_SCHEMA,
                "record_type": "descriptive_target_outcome",
                "wave": wave,
                "lane": {
                    "status": "DESCRIPTIVE_NONVOTING",
                    "allowed_as_decision_feature": False,
                },
                "outcome": {"target_guid": target, "death_clock": {"censored": True}},
            }
        )
    rows.append(
        {
            "schema": TEAM_SCHEMA,
            "record_type": "descriptive_wave_outcome",
            "wave": wave,
            "lane": {
                "status": "DESCRIPTIVE_NONVOTING",
                "allowed_as_decision_feature": False,
            },
            "outcome": {"event_count": len(exact)},
        }
    )
    return rows


def _write_partition(path: Path, rows: list[dict[str, object]]) -> dict[str, object]:
    payload = b"".join(_canonical_bytes(row) + b"\n" for row in rows)
    with path.open("wb") as raw:
        with gzip.GzipFile(
            filename="", fileobj=raw, mode="wb", compresslevel=9, mtime=0
        ) as compressed:
            compressed.write(payload)
    wave_count = sum(row.get("record_type") == "wave_model_header" for row in rows)
    return {
        "instance_id": rows[0]["wave"]["instance_id"],
        "partition": path.name,
        "logical_content_sha256": hashlib.sha256(payload).hexdigest(),
        "compressed_file_sha256": _sha256_file(path),
        "compressed_size_bytes": path.stat().st_size,
        "wave_count": wave_count,
        "voting_ready": False,
        "exact_trace_status": "DESCRIPTIVE_NONVOTING",
    }


def _fixture(root: Path) -> Path:
    source = root / "team-model"
    source.mkdir(parents=True)
    node_to_component = {
        "instance-a": COMPONENT_A,
        "player-focal-a": COMPONENT_A,
        "player-arms": COMPONENT_A,
        "player-mage": COMPONENT_A,
        "instance-b": COMPONENT_B,
        "player-focal-b": COMPONENT_B,
    }
    rows_a = _wave_rows(
        instance=INSTANCE_A,
        ordinal=1,
        contamination="POSTFIX_KNOWN_CLEAN",
        focal=FOCAL_A,
        instance_node="instance-a",
        focal_node="player-focal-a",
        include_team=True,
    ) + _wave_rows(
        instance=INSTANCE_A,
        ordinal=2,
        contamination="NO_KNOWN_RULE_MATCH",
        focal=FOCAL_A,
        instance_node="instance-a",
        focal_node="player-focal-a",
        include_team=True,
    )
    rows_b = _wave_rows(
        instance=INSTANCE_B,
        ordinal=1,
        contamination="SUSPECT_36YD_RANGE_BUG",
        focal=FOCAL_B,
        instance_node="instance-b",
        focal_node="player-focal-b",
        include_team=False,
    )
    entry_a = _write_partition(source / "instance-a.jsonl.gz", rows_a)
    entry_b = _write_partition(source / "instance-b.jsonl.gz", rows_b)
    core = {
        "schema": TEAM_SCHEMA,
        "schema_version": 1,
        "kind": "chronicle_team_wave_model_manifest",
        "implementation_revision": TEAM_MODEL_IMPLEMENTATION_REVISION,
        "claim_boundary": {
            "exact_trace_status": "DESCRIPTIVE_NONVOTING",
            "exact_trace_can_vote": False,
            "comparison_ready": False,
        },
        "split_graph": {
            "required_split_unit": "connected component",
            "row_random_split_allowed": False,
            "same_player_or_guild_can_cross_folds": False,
            "node_to_component": [
                {"node_id": node, "component_id": component}
                for node, component in sorted(node_to_component.items())
            ],
        },
        "partitions": [entry_a, entry_b],
    }
    manifest = {
        **core,
        "content_address": {
            "algorithm": "sha256",
            "scope": "canonical JSON document excluding content_address",
            "sha256": _sha256_json(core),
        },
    }
    path = source / "manifest.json"
    path.write_bytes(_canonical_bytes(manifest) + b"\n")
    return path


class ChronicleTeamBackgroundGeneratorV1Tests(unittest.TestCase):
    def _build(self, root: Path):
        return build_chronicle_team_background_generator(
            team_model_manifest_path=_fixture(root),
            output_directory=root / "generator",
        )

    def test_stale_team_model_revision_is_rejected_before_background_build(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path = _fixture(root)
            manifest = json.loads(manifest_path.read_text("utf-8"))
            manifest["implementation_revision"] = "stale-cutoff-revision"
            core = {
                key: value
                for key, value in manifest.items()
                if key != "content_address"
            }
            manifest["content_address"]["sha256"] = _sha256_json(core)
            manifest_path.write_bytes(_canonical_bytes(manifest) + b"\n")
            with self.assertRaisesRegex(
                ChronicleTeamBackgroundGeneratorError,
                "input manifest must be",
            ):
                build_chronicle_team_background_generator(
                    team_model_manifest_path=manifest_path,
                    output_directory=root / "generator",
                )

    def test_compile_preserves_components_contamination_and_specializations(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            result = self._build(root)
            manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
            validate_background_generator_manifest(manifest)
            self.assertEqual(2, manifest["summary"]["component_count"])
            self.assertEqual(3, manifest["summary"]["block_count"])
            self.assertEqual(2, manifest["summary"]["training_eligible_block_count"])
            suspect = next(
                row
                for row in manifest["blocks"]
                if row["component_id"] == COMPONENT_B
            )
            self.assertFalse(suspect["training_eligible"])
            block_path = result.manifest.parent / manifest["blocks"][0]["block"]
            with gzip.open(block_path, "rt", encoding="utf-8") as handle:
                block = json.load(handle)
            arms = next(
                track
                for track in block["player_tracks"]
                if track["player"]["guid"] == ARMS
            )
            self.assertEqual("WARRIOR_ARMS", arms["specialization_partition"])
            self.assertEqual(
                "ARMS_COMMON_ACTION_DIAGNOSTIC_ONLY_NONVOTING",
                arms["historical_warrior_lane"],
            )
            self.assertFalse(arms["fury_policy_training_eligible"])
            self.assertEqual(45, block["historical_duration_ms"])
            self.assertFalse(manifest["claim_boundary"]["comparison_ready"])
            contamination = manifest["contamination_contract"]
            self.assertEqual(
                "south_north_36yd_started_at_v2_20260903_noon",
                contamination["rule_version"],
            )
            self.assertEqual(
                "2026-09-03T00:00:00+08:00",
                contamination["pre_fix_suspect_before_local"],
            )
            self.assertEqual(
                "2026-09-03T00:00:00+08:00",
                contamination["boundary_uncertain_at_or_after_local"],
            )
            self.assertEqual(
                "2026-09-03T12:00:00+08:00",
                contamination["postfix_known_clean_at_or_after_local"],
            )
            self.assertIn(
                "RANGE_BUG_BOUNDARY_UNCERTAIN_NONVOTING",
                contamination["nontraining_statuses"],
            )
            self.assertIn(
                "not a claim about the exact patch instant",
                contamination["boundary_provenance"]["safe_floor_policy"],
            )

    def test_seeded_draw_is_repeatable_and_removes_full_focal_lane(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            result = self._build(root)
            first = draw_background_schedule(
                generator_manifest_path=result.manifest,
                component_id=COMPONENT_A,
                focal_player_guid=FOCAL_A,
                seed=20260911,
                draw_index=7,
                retarget_mode=NO_RETARGET_CONTROL,
            )
            second = draw_background_schedule(
                generator_manifest_path=result.manifest,
                component_id=COMPONENT_A,
                focal_player_guid=FOCAL_A,
                seed=20260911,
                draw_index=7,
                retarget_mode=NO_RETARGET_CONTROL,
            )
            self.assertEqual(first, second)
            self.assertEqual(
                first["content_address"]["sha256"],
                second["content_address"]["sha256"],
            )
            self.assertEqual(2, first["draw_identity"]["population_size"])
            self.assertEqual(2, first["focal_leave_one_out"]["excluded_event_count"])
            self.assertEqual(50, first["focal_leave_one_out"]["excluded_numeric_damage"])
            self.assertTrue(
                all(
                    event.get("source_player", {}).get("guid") != FOCAL_A
                    for event in first["schedule"]
                    if event.get("source_player") is not None
                )
            )
            self.assertEqual(2, first["unattributed_branch"]["event_count"])
            self.assertEqual(COMPONENT_A, first["component_id"])
            self.assertTrue(first["source_block"]["training_eligible"])
            self.assertIn(
                first["source_block"]["contamination"]["status"],
                ("POSTFIX_KNOWN_CLEAN", "NO_KNOWN_RULE_MATCH"),
            )
            self.assertEqual(
                INSTANCE_A, first["source_block"]["source_wave"]["instance_id"]
            )
            written = write_background_draw(first, root / "draws")
            self.assertIn(first["content_address"]["sha256"], written.name)

    def test_dynamic_two_target_ttk_cancels_only_future_dead_target_hits(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            result = self._build(root)
            draw = draw_background_schedule(
                generator_manifest_path=result.manifest,
                component_id=COMPONENT_A,
                focal_player_guid=FOCAL_A,
                seed=3,
                draw_index=0,
                retarget_mode=NO_RETARGET_CONTROL,
            )
            replay = replay_background_schedule(
                draw=draw,
                target_health={TARGET_A: 120, TARGET_B: 100},
                candidate_damage_events=[
                    {
                        "relative_ms": 15,
                        "target_guid": TARGET_A,
                        "amount": 40,
                        "event_id": "candidate-a",
                    }
                ],
            )
            self.assertEqual(1, len(replay["death_clocks"]))
            death = replay["death_clocks"][0]
            self.assertEqual(TARGET_A, death["target_guid"])
            self.assertEqual(15, death["relative_ms"])
            self.assertEqual("CANDIDATE", death["killing_stream"])
            future = next(
                row
                for row in replay["cancelled_events"]
                if row.get("spell", {}).get("name") == "Future hit"
            )
            self.assertEqual("TARGET_ALREADY_DEAD", future["cancellation_reason"])
            self.assertEqual(125, replay["damage_totals"][TARGET_A]["combined"])
            self.assertEqual(60, replay["damage_totals"][TARGET_B]["combined"])
            emitted = list(iter_runtime_schedule(replay))
            self.assertTrue(any(row["runtime_status"] == "EMITTED_ACTION" for row in emitted))
            self.assertFalse(
                any(
                    row.get("spell", {}).get("name") == "Historical DEAD value"
                    for row in replay["processed_events"]
                )
            )

    def test_unknown_retarget_is_explicit_and_never_guessed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            result = self._build(root)
            draw = draw_background_schedule(
                generator_manifest_path=result.manifest,
                component_id=COMPONENT_A,
                focal_player_guid=FOCAL_A,
                seed=5,
                draw_index=0,
            )
            self.assertEqual("RETARGET_UNIDENTIFIED_NONVOTING", draw["status"])
            self.assertFalse(draw["claim_boundary"]["runtime_schedule_eligible"])
            replay = replay_background_schedule(
                draw=draw,
                target_health={TARGET_A: 120, TARGET_B: 100},
            )
            self.assertEqual("RETARGET_UNIDENTIFIED_NONVOTING", replay["status"])
            damage = [
                row
                for row in replay["processed_events"]
                if row.get("kind") == "DAMAGE"
            ]
            self.assertTrue(damage)
            self.assertTrue(
                all(row.get("cancellation_reason") == "RETARGET_UNIDENTIFIED" for row in damage)
            )
            late_action = next(
                row
                for row in draw["schedule"]
                if row.get("spell", {}).get("name") == "Late unknown action"
            )
            self.assertIsNone(late_action["runtime_target_guid"])
            self.assertEqual(
                "RETARGET_UNIDENTIFIED", late_action["runtime_target_status"]
            )

    def test_component_pool_never_crosses_and_suspect_component_cannot_draw(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            result = self._build(root)
            for seed in range(12):
                draw = draw_background_schedule(
                    generator_manifest_path=result.manifest,
                    component_id=COMPONENT_A,
                    focal_player_guid=FOCAL_A,
                    seed=seed,
                    draw_index=seed,
                    retarget_mode=NO_RETARGET_CONTROL,
                )
                self.assertEqual(COMPONENT_A, draw["component_id"])
                self.assertEqual(
                    INSTANCE_A,
                    draw["source_block"]["source_wave"]["instance_id"],
                )
            with self.assertRaisesRegex(
                ChronicleTeamBackgroundGeneratorError, "no clean training block"
            ):
                draw_background_schedule(
                    generator_manifest_path=result.manifest,
                    component_id=COMPONENT_B,
                    focal_player_guid=FOCAL_B,
                    seed=1,
                    draw_index=0,
                    retarget_mode=NO_RETARGET_CONTROL,
                )

    def test_draw_and_manifest_content_addresses_fail_closed_on_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            result = self._build(root)
            draw = draw_background_schedule(
                generator_manifest_path=result.manifest,
                component_id=COMPONENT_A,
                focal_player_guid=FOCAL_A,
                seed=9,
                draw_index=2,
                retarget_mode=NO_RETARGET_CONTROL,
            )
            validate_background_draw(draw)
            tampered = deepcopy(draw)
            tampered["schedule"][0]["relative_ms"] += 1
            with self.assertRaisesRegex(
                ChronicleTeamBackgroundGeneratorError, "content address mismatch"
            ):
                validate_background_draw(tampered)
            manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
            manifest["summary"]["comparison_ready"] = True
            with self.assertRaisesRegex(
                ChronicleTeamBackgroundGeneratorError, "content address mismatch"
            ):
                validate_background_generator_manifest(manifest)

            stale_contract = json.loads(result.manifest.read_text(encoding="utf-8"))
            stale_contract["contamination_contract"] = {
                "pre_september_south_north_default_training_eligible": False,
                "pre_september_south_north_voting_eligible": False,
            }
            stale_core = {
                key: value
                for key, value in stale_contract.items()
                if key != "content_address"
            }
            stale_contract["content_address"]["sha256"] = _sha256_json(stale_core)
            with self.assertRaisesRegex(
                ChronicleTeamBackgroundGeneratorError,
                "contamination contract is stale",
            ):
                validate_background_generator_manifest(stale_contract)

    def test_draw_and_runtime_replay_reject_stale_or_missing_revision(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            result = self._build(root)
            draw = draw_background_schedule(
                generator_manifest_path=result.manifest,
                component_id=COMPONENT_A,
                focal_player_guid=FOCAL_A,
                seed=11,
                draw_index=0,
                retarget_mode=NO_RETARGET_CONTROL,
            )
            self.assertEqual(IMPLEMENTATION_REVISION, draw["implementation_revision"])

            replay = replay_background_schedule(
                draw=draw,
                target_health={TARGET_A: 120, TARGET_B: 100},
            )
            self.assertEqual(
                IMPLEMENTATION_REVISION, replay["implementation_revision"]
            )
            self.assertTrue(list(iter_runtime_schedule(replay)))

            for revision in ("stale-pre-boundary-revision", None):
                stale_draw = deepcopy(draw)
                if revision is None:
                    stale_draw.pop("implementation_revision")
                else:
                    stale_draw["implementation_revision"] = revision
                stale_draw_core = {
                    key: value
                    for key, value in stale_draw.items()
                    if key != "content_address"
                }
                stale_draw["content_address"]["sha256"] = _sha256_json(
                    stale_draw_core
                )
                with self.assertRaisesRegex(
                    ChronicleTeamBackgroundGeneratorError,
                    "draw implementation revision",
                ):
                    validate_background_draw(stale_draw)

                stale_replay = deepcopy(replay)
                if revision is None:
                    stale_replay.pop("implementation_revision")
                else:
                    stale_replay["implementation_revision"] = revision
                stale_replay_core = {
                    key: value
                    for key, value in stale_replay.items()
                    if key != "content_address"
                }
                stale_replay["content_address"]["sha256"] = _sha256_json(
                    stale_replay_core
                )
                with self.assertRaisesRegex(
                    ChronicleTeamBackgroundGeneratorError,
                    "runtime replay implementation revision",
                ):
                    list(iter_runtime_schedule(stale_replay))


if __name__ == "__main__":
    unittest.main()
