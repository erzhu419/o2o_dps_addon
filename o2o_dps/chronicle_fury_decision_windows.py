"""Build conservative Fury behavior-decision windows from trajectory v1.

The input is the already player-scoped Chronicle partial trajectory.  Each
output row is anchored at one observed START candidate.  The artifact is useful
for observable behavior analysis, but it is deliberately not a full-state BC or
offline-RL transition dataset.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Iterable, Sequence


SCHEMA_VERSION = 1
SCHEMA_NAME = "chronicle_fury_decision_window/v1"
DEFAULT_DATA_ROOT = Path(__file__).resolve().parents[1] / "offline_data"
DEFAULT_REGISTRY = (
    Path(__file__).resolve().parents[1]
    / "mechanics"
    / "registry"
    / "turtle_1_18_1"
    / "warrior_fury.json"
)
DEFAULT_OUTPUT = (
    DEFAULT_DATA_ROOT
    / "derived"
    / "chronicle_fury_decision_windows"
    / "v1"
)
MISSING_STATE_FIELDS = (
    "absolute_rage",
    "gcd_remaining_ms",
    "cooldown_remaining_ms",
    "mainhand_swing_remaining_ms",
    "offhand_swing_remaining_ms",
    "queue_intent",
    "client_keypress_ms",
    "target_hp",
    "player_hp",
    "boss_phase",
    "target_count",
    "stance",
    "range",
    "behind_target",
    "gear_item_ids",
    "exact_talent_ranks",
)


class DecisionWindowError(RuntimeError):
    """The inputs cannot support a trustworthy decision-window artifact."""


@dataclass(frozen=True)
class DecisionWindowResult:
    start_candidate_count: int
    observable_behavior_labels: int
    trajectory: Path
    manifest: Path

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "start_candidate_count": self.start_candidate_count,
            "observable_behavior_labels": self.observable_behavior_labels,
            "trajectory": str(self.trajectory),
            "manifest": str(self.manifest),
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise DecisionWindowError(f"cannot read {label} {path}: {error}") from error
    if not isinstance(value, dict):
        raise DecisionWindowError(f"{label} is not a JSON object: {path}")
    return value


def _read_trajectory(
    path: Path,
    encounter: str,
    *,
    expected_instance: str,
    expected_player_guid: str,
    expected_player_name: str,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", buffering=1024 * 1024) as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as error:
                    raise DecisionWindowError(
                        f"invalid trajectory JSON at {path}:{line_number}: {error}"
                    ) from error
                if not isinstance(record, dict):
                    raise DecisionWindowError(
                        f"trajectory row is not an object at {path}:{line_number}"
                    )
                if record.get("schema") != "chronicle_fury_partial_trajectory/v1":
                    raise DecisionWindowError(
                        f"unexpected trajectory schema at {path}:{line_number}"
                    )
                identity = record.get("identity")
                if not isinstance(identity, dict):
                    raise DecisionWindowError(
                        f"trajectory row lacks identity at {path}:{line_number}"
                    )
                if str(identity.get("canonical_instance_id") or "").casefold() != (
                    expected_instance.casefold()
                ):
                    raise DecisionWindowError(
                        f"trajectory row instance does not match manifest at {path}:{line_number}"
                    )
                if _guid_key(identity.get("player_guid")) != _guid_key(
                    expected_player_guid
                ):
                    raise DecisionWindowError(
                        f"trajectory row player GUID does not match manifest at {path}:{line_number}"
                    )
                if str(identity.get("player_name") or "").casefold() != (
                    expected_player_name.casefold()
                ):
                    raise DecisionWindowError(
                        f"trajectory row player name does not match manifest at {path}:{line_number}"
                    )
                if not str(identity.get("encounter_id") or "").strip():
                    raise DecisionWindowError(
                        f"trajectory row lacks encounter identity at {path}:{line_number}"
                    )
                if identity.get("encounter_id") == encounter:
                    selected.append(record)
    except (OSError, UnicodeError) as error:
        raise DecisionWindowError(f"cannot read trajectory {path}: {error}") from error
    if not selected:
        raise DecisionWindowError(f"trajectory has no rows for encounter {encounter}")
    selected.sort(
        key=lambda value: (
            int((value.get("event") or {}).get("event_index") or 0),
            int((value.get("event") or {}).get("csv_line") or 0),
        )
    )
    return selected


def _guid_key(value: Any) -> str:
    return str(value or "").strip().casefold()


def _readiness_group(
    report: dict[str, Any], manifest: dict[str, Any], encounter: str
) -> dict[str, Any]:
    player = manifest.get("player")
    source = manifest.get("source")
    if not isinstance(player, dict) or not isinstance(source, dict):
        raise DecisionWindowError("trajectory manifest lacks player/source objects")
    guid = _guid_key(player.get("guid"))
    player_name = str(player.get("name") or "").casefold()
    normalized_name = Path(str(source.get("normalized_file") or "")).name
    matches = [
        item
        for item in report.get("player_encounters") or []
        if isinstance(item, dict)
        and item.get("encounter_id") == encounter
        and _guid_key(item.get("player_guid")) == guid
        and str(item.get("player_name") or "").casefold() == player_name
        and Path(str(item.get("normalized_file") or "")).name == normalized_name
    ]
    if len(matches) != 1:
        raise DecisionWindowError(
            "readiness report must contain exactly one matching player/encounter"
        )
    identity = matches[0].get("identity")
    if not isinstance(identity, dict) or not identity.get("verified"):
        raise DecisionWindowError("readiness identity is not verified; refusing to publish")
    return matches[0]


def _registry_actions(path: Path) -> tuple[dict[int, dict[str, str]], dict[str, Any]]:
    registry = _load_object(path, "Warrior mechanics registry")
    actions: dict[int, dict[str, str]] = {}
    for mechanic in registry.get("mechanics") or []:
        if not isinstance(mechanic, dict):
            continue
        implementation = mechanic.get("implementation")
        implementation = implementation if isinstance(implementation, dict) else {}
        ids = {
            value
            for key, value in implementation.items()
            if key in ("spell_id", "wrapper_spell_id") and isinstance(value, int)
        }
        ids.update(
            value for value in implementation.get("spell_ids") or [] if isinstance(value, int)
        )
        if implementation.get("queue_tag") == 1 or implementation.get(
            "replaces_next_main_hand_swing"
        ):
            lane = "on_swing_unknown_intent"
        elif implementation.get("consumes_gcd") is False:
            lane = "off_gcd"
        elif any(
            key in implementation
            for key in (
                "gcd_seconds",
                "base_gcd_seconds",
                "consumes_gcd",
                "cooldown_seconds",
            )
        ):
            lane = "gcd"
        else:
            lane = "unknown"
        for spell_id in ids:
            actions[spell_id] = {
                "lane": lane,
                "policy_action_key": str(mechanic.get("key") or ""),
            }
    return actions, {
        "path": str(path.resolve()),
        "schema_version": registry.get("schema_version"),
        "role": "action-lane classification only; no historical state is filled",
    }


def _event(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("event")
    return value if isinstance(value, dict) else {}


def _fields(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("fields")
    return value if isinstance(value, dict) else {}


def _provenance(record: dict[str, Any], field: str) -> dict[str, Any] | None:
    value = record.get("field_provenance")
    if not isinstance(value, dict):
        return None
    item = value.get("fields." + field) or value.get(field)
    return item if isinstance(item, dict) else None


def _aura_key(fields: dict[str, Any]) -> tuple[str, str, str]:
    actor = str(fields.get("target_guid") or fields.get("target_name") or "")
    spell = str(fields.get("spell_id") or fields.get("spell_name") or "")
    source = str(fields.get("source_guid") or fields.get("source_name") or "")
    return actor, spell, source


def _numeric(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return 0.0


def _window_observation(records: list[dict[str, Any]]) -> dict[str, Any]:
    outgoing_damage = 0.0
    rage_gain = 0.0
    auto_attacks = 0
    for record in records:
        kind = record.get("record_kind")
        fields = _fields(record)
        if kind == "reward_event" and fields.get("direction") == "outgoing":
            outgoing_damage += _numeric(fields.get("damage_amount"))
            if fields.get("spell_id") == 6603 or str(
                fields.get("spell_name") or ""
            ).casefold() == "auto attack":
                auto_attacks += 1
        elif (
            kind == "resource_event"
            and str(fields.get("resource") or "").casefold() == "rage"
            and str(fields.get("direction") or "").casefold() == "gain"
        ):
            rage_gain += _numeric(fields.get("amount_chronicle_units"))
    return {
        "outgoing_damage_observed": outgoing_damage,
        "rage_gain_chronicle_units": rage_gain,
        "auto_attack_rows": auto_attacks,
        "causal_reward": None,
        "causal_attribution": "MISSING",
    }


def _result_map(records: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    results: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if record.get("record_kind") != "action_result":
            continue
        fields = _fields(record)
        candidate_ids: list[str] = []
        candidate_id = fields.get("matched_candidate_action_id")
        if fields.get("association_status") == "unique" and candidate_id:
            candidate_ids.append(str(candidate_id))
        elif fields.get("association_status") == "ambiguous":
            candidate_ids.extend(
                str(value) for value in fields.get("candidate_action_ids") or [] if value
            )
        for value in candidate_ids:
            results.setdefault(value, []).append(record)
    return results


def _output_base(manifest: dict[str, Any], encounter: str) -> str:
    identity = manifest.get("identity") or {}
    player = manifest.get("player") or {}
    instance = str(identity.get("canonical_instance_id") or "instance")
    guid = str(player.get("guid") or "player").replace("0x", "", 1)
    safe = lambda value: re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
    return f"{safe(instance)}__{safe(guid)}__{safe(encounter)}"


def _is_uuid(value: Any) -> bool:
    return bool(
        re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            str(value or ""),
        )
    )


def build_decision_windows(
    *,
    trajectory: str | Path,
    trajectory_manifest: str | Path,
    readiness_report: str | Path,
    encounter: str,
    registry: str | Path = DEFAULT_REGISTRY,
    output_dir: str | Path = DEFAULT_OUTPUT,
) -> DecisionWindowResult:
    trajectory_path = Path(trajectory).expanduser().resolve()
    manifest_path = Path(trajectory_manifest).expanduser().resolve()
    readiness_path = Path(readiness_report).expanduser().resolve()
    registry_path = Path(registry).expanduser().resolve()
    output_path = Path(output_dir).expanduser().resolve()
    encounter_id = encounter.strip()
    if not encounter_id:
        raise DecisionWindowError("encounter must not be empty")

    manifest = _load_object(manifest_path, "trajectory manifest")
    if manifest.get("kind") != "chronicle_fury_partial_trajectory_manifest":
        raise DecisionWindowError("input manifest is not a Fury partial trajectory manifest")
    player = manifest.get("player") or {}
    classes = {str(value).upper() for value in player.get("info_classes") or []}
    specs = {str(value).casefold() for value in player.get("leaderboard_observed_specs") or []}
    if "WARRIOR" not in classes or "fury" not in specs:
        raise DecisionWindowError("input is not an identity-verified Fury Warrior trajectory")
    if not _is_uuid((manifest.get("identity") or {}).get("canonical_instance_id")):
        raise DecisionWindowError(
            "bounded v1 requires a verified canonical instance UUID; slug-only identity is refused"
        )

    readiness = _load_object(readiness_path, "reconstruction-readiness report")
    readiness_group = _readiness_group(readiness, manifest, encounter_id)
    active_actions, registry_info = _registry_actions(registry_path)
    canonical_instance_id = str(
        (manifest.get("identity") or {}).get("canonical_instance_id") or ""
    )
    player_guid = str(player.get("guid") or "")
    player_name = str(player.get("name") or "")
    if not player_guid or not player_name:
        raise DecisionWindowError("trajectory manifest lacks player GUID or name")
    records = _read_trajectory(
        trajectory_path,
        encounter_id,
        expected_instance=canonical_instance_id,
        expected_player_guid=player_guid,
        expected_player_name=player_name,
    )
    candidates = [
        (index, record)
        for index, record in enumerate(records)
        if record.get("record_kind") == "candidate_action"
    ]
    if not candidates:
        raise DecisionWindowError("selected encounter has no START candidate actions")
    results = _result_map(records)

    rage_gain_total = 0.0
    rage_loss_total = 0.0
    rage_gain_rows = 0
    rage_loss_rows = 0
    last_rage_event_index: int | None = None
    last_auto_attack_offset: int | None = None
    last_auto_attack_event_index: int | None = None
    aura_ledger: dict[tuple[str, str, str], dict[str, Any]] = {}
    last_aura_event_index: int | None = None
    damaged_targets: set[str] = set()
    last_damaged_target_event_index: int | None = None
    recent_actions: list[dict[str, Any]] = []
    output_records: list[dict[str, Any]] = []
    candidate_positions = {index for index, _ in candidates}
    candidate_next = {
        index: (candidates[position + 1][0] if position + 1 < len(candidates) else len(records))
        for position, (index, _) in enumerate(candidates)
    }
    state_future_leakage_count = 0
    non_null_missing_fields = 0

    for index, record in enumerate(records):
        kind = record.get("record_kind")
        fields = _fields(record)
        event = _event(record)
        if kind == "resource_event" and str(fields.get("resource") or "").casefold() == "rage":
            amount = _numeric(fields.get("amount_chronicle_units"))
            direction = str(fields.get("direction") or "").casefold()
            if direction == "gain":
                rage_gain_total += amount
                rage_gain_rows += 1
                last_rage_event_index = int(event.get("event_index") or 0)
            elif direction == "loss":
                rage_loss_total += amount
                rage_loss_rows += 1
                last_rage_event_index = int(event.get("event_index") or 0)
        elif kind == "reward_event":
            if fields.get("direction") == "outgoing" and fields.get("target_guid"):
                damaged_targets.add(str(fields["target_guid"]))
                last_damaged_target_event_index = int(event.get("event_index") or 0)
            if fields.get("direction") == "outgoing" and (
                fields.get("spell_id") == 6603
                or str(fields.get("spell_name") or "").casefold() == "auto attack"
            ):
                last_auto_attack_offset = int(event.get("offset_ms") or 0)
                last_auto_attack_event_index = int(event.get("event_index") or 0)
        elif kind == "aura_event":
            key = _aura_key(fields)
            change = str(fields.get("aura_change") or "").casefold()
            if "remove" in change:
                aura_ledger.pop(key, None)
            else:
                aura_ledger[key] = {
                    "target_guid": fields.get("target_guid"),
                    "target_name": fields.get("target_name"),
                    "source_guid": fields.get("source_guid"),
                    "spell_id": fields.get("spell_id"),
                    "spell_name": fields.get("spell_name"),
                    "change": fields.get("aura_change"),
                    "stacks": fields.get("stacks"),
                    "event_index": event.get("event_index"),
                }
            last_aura_event_index = int(event.get("event_index") or 0)
        elif kind == "action_result":
            result_fields = fields
            candidate_id = result_fields.get("matched_candidate_action_id")
            if result_fields.get("association_status") == "unique" and candidate_id:
                recent_actions.append(
                    {
                        "candidate_action_id": candidate_id,
                        "spell_id": result_fields.get("spell_id"),
                        "spell_name": result_fields.get("spell_name"),
                        "result_status": result_fields.get("result_status"),
                        "result_event_index": event.get("event_index"),
                        "result_offset_ms": event.get("offset_ms"),
                    }
                )
                recent_actions = recent_actions[-10:]

        if index not in candidate_positions:
            continue

        candidate_id = str(fields.get("candidate_action_id") or "")
        linked = results.get(candidate_id, [])
        linked_result = linked[0] if len(linked) == 1 else None
        linked_fields = _fields(linked_result) if linked_result else {}
        linked_event = _event(linked_result) if linked_result else {}
        association = (
            str(linked_fields.get("association_status"))
            if linked_result
            else "ambiguous"
            if len(linked) > 1
            else "unlinked"
        )
        result_status = str(linked_fields.get("result_status") or "missing")
        spell_id = fields.get("spell_id") if isinstance(fields.get("spell_id"), int) else None
        catalog = active_actions.get(spell_id) if spell_id is not None else None
        lane = catalog["lane"] if catalog else "unknown"
        behavior_label = (
            association == "unique"
            and result_status == "succeeded"
            and catalog is not None
            and lane != "on_swing_unknown_intent"
        )
        start_index = int(event.get("event_index") or 0)
        start_offset = int(event.get("offset_ms") or 0)
        last_swing_elapsed = (
            start_offset - last_auto_attack_offset
            if last_auto_attack_offset is not None and start_offset >= last_auto_attack_offset
            else None
        )
        talent_provenance = _provenance(record, "state_talent_tree") or {
            "kind": "MISSING",
            "event_index": None,
            "note": "no prior parseable INFO talent-tree totals",
        }
        gear_provenance = _provenance(record, "state_gear_slot_count") or {
            "kind": "MISSING",
            "event_index": None,
            "note": "no prior parseable INFO gear-slot count",
        }
        state_provenance = {
            "combat_time_ms": {
                "kind": "OBSERVED",
                "event_index": start_index,
                "note": "current START offset anchor",
            },
            "talent_tree_point_totals": talent_provenance,
            "gear_slot_count": gear_provenance,
            "rage_gain_total_chronicle_units": {
                "kind": "RECONSTRUCTED",
                "event_index": last_rage_event_index,
                "note": "sum of prior exported Gain Rage rows; not current rage",
            },
            "rage_loss_total_chronicle_units": {
                "kind": "RECONSTRUCTED" if rage_loss_rows else "MISSING",
                "event_index": last_rage_event_index if rage_loss_rows else None,
                "note": "sum of prior exported Loss Rage rows; absence is not proof of zero spend",
            },
            "last_auto_attack_elapsed_ms": {
                "kind": "RECONSTRUCTED" if last_swing_elapsed is not None else "MISSING",
                "event_index": last_auto_attack_event_index,
                "note": "elapsed since an observed Auto Attack; not MH/OH remaining",
            },
            "player_involved_aura_event_ledger": {
                "kind": "RECONSTRUCTED",
                "event_index": last_aura_event_index,
                "note": "prior event ledger only; initial state and durations are incomplete",
            },
            "damaged_target_guids_seen": {
                "kind": "RECONSTRUCTED",
                "event_index": last_damaged_target_event_index,
                "note": "encounter-to-date outgoing damage targets; not simultaneous target count",
            },
        }
        for evidence in state_provenance.values():
            if isinstance(evidence, dict):
                evidence_index = evidence.get("event_index")
                if isinstance(evidence_index, int) and evidence_index > start_index:
                    state_future_leakage_count += 1

        missing_state = {name: None for name in MISSING_STATE_FIELDS}
        missing_mask = {name: False for name in MISSING_STATE_FIELDS}
        non_null_missing_fields += sum(
            1 for name in MISSING_STATE_FIELDS if missing_state[name] is not None
        )
        window = _window_observation(records[index + 1 : candidate_next[index]])
        output_records.append(
            {
                "schema_version": SCHEMA_VERSION,
                "schema": SCHEMA_NAME,
                "identity": {
                    "instance_id": (manifest.get("identity") or {}).get(
                        "canonical_instance_id"
                    ),
                    "encounter_id": encounter_id,
                    "player_guid": player.get("guid"),
                    "player_name": player.get("name"),
                    "leaderboard_observed_specs": player.get(
                        "leaderboard_observed_specs"
                    ),
                },
                "decision": {
                    "decision_id": candidate_id,
                    "start_event_index": start_index,
                    "start_csv_line": event.get("csv_line"),
                    "start_offset_ms": start_offset,
                    "action": {
                        "spell_id": spell_id,
                        "spell_name": fields.get("spell_name"),
                        "target_guid": fields.get("target_guid"),
                        "target_name": fields.get("target_name"),
                        "policy_action_key": (
                            catalog["policy_action_key"] if catalog else None
                        ),
                        "catalog_status": "MAPPED_ACTIVE" if catalog else "UNMAPPED",
                    },
                    "association": association,
                    "result": result_status,
                    "result_event_index": linked_event.get("event_index"),
                    "result_offset_ms": linked_event.get("offset_ms"),
                    "start_to_result_ms": linked_fields.get("start_to_result_ms"),
                    "action_semantics": "server_observed_candidate",
                    "lane": lane,
                    "observable_behavior_label": behavior_label,
                    "queue_intent_label": False,
                    "full_state_bc_eligible": False,
                    "offline_rl_eligible": False,
                },
                "state_before": {
                    "combat_time_ms": start_offset,
                    "recent_uniquely_linked_server_actions": list(recent_actions),
                    "rage_gain_total_chronicle_units": rage_gain_total,
                    "rage_gain_rows_observed": rage_gain_rows,
                    "rage_loss_total_chronicle_units": (
                        rage_loss_total if rage_loss_rows else None
                    ),
                    "rage_loss_rows_observed": rage_loss_rows,
                    "rage_scale_to_wow": None,
                    "last_auto_attack_elapsed_ms": last_swing_elapsed,
                    "player_involved_aura_event_ledger": {
                        "initial_state_complete": False,
                        "duration_complete": False,
                        "entries": [
                            aura_ledger[key] for key in sorted(aura_ledger)
                        ],
                    },
                    "damaged_target_guids_seen": sorted(damaged_targets),
                    "talent_tree_point_totals": fields.get("state_talent_tree"),
                    "gear_slot_count": fields.get("state_gear_slot_count"),
                    **missing_state,
                },
                "state_mask": {
                    "combat_time_ms": True,
                    "rage_gain_total_chronicle_units": True,
                    "rage_gain_rows_observed": True,
                    "rage_loss_total_chronicle_units": rage_loss_rows > 0,
                    "rage_loss_rows_observed": True,
                    "rage_scale_to_wow": False,
                    "last_auto_attack_elapsed_ms": last_swing_elapsed is not None,
                    "player_involved_aura_event_ledger": True,
                    "damaged_target_guids_seen": True,
                    "talent_tree_point_totals": fields.get("state_talent_tree") is not None,
                    "gear_slot_count": fields.get("state_gear_slot_count") is not None,
                    **missing_mask,
                },
                "state_provenance": state_provenance,
                "window_until_next_start_candidate": window,
            }
        )

    if state_future_leakage_count:
        raise DecisionWindowError(
            "future state evidence detected in "
            f"{state_future_leakage_count} fields"
        )
    if non_null_missing_fields:
        raise DecisionWindowError(
            f"MISSING state fields contain {non_null_missing_fields} non-null values"
        )

    output_path.mkdir(parents=True, exist_ok=True)
    base = _output_base(manifest, encounter_id)
    trajectory_output = output_path / f"{base}.jsonl"
    manifest_output = output_path / f"{base}.manifest.json"
    trajectory_temporary: Path | None = None
    manifest_temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{base}__",
            suffix=".jsonl.tmp",
            dir=output_path,
            delete=False,
        ) as handle:
            trajectory_temporary = Path(handle.name)
            for record in output_records:
                json.dump(record, handle, ensure_ascii=False, separators=(",", ":"))
                handle.write("\n")

        counts = Counter(
            "observable" if value["decision"]["observable_behavior_label"] else "excluded"
            for value in output_records
        )
        catalog_counts = Counter(
            value["decision"]["action"]["catalog_status"]
            for value in output_records
        )
        output_manifest = {
            "schema_version": SCHEMA_VERSION,
            "schema": SCHEMA_NAME,
            "kind": "chronicle_fury_decision_window_manifest",
            "generated_at": _utc_now(),
            "identity": output_records[0]["identity"],
            "inputs": {
                "trajectory": str(trajectory_path),
                "trajectory_manifest": str(manifest_path),
                "readiness_report": str(readiness_path),
                "registry": registry_info,
            },
            "readiness": {
                "identity_verified": readiness_group["identity"]["verified"],
                "full_state_ready": readiness_group["full_state_ready"],
                "full_state_blockers": readiness_group["full_state_blockers"],
            },
            "output": {
                "trajectory": str(trajectory_output),
                "start_candidate_count": len(output_records),
                "mapped_active_candidate_count": catalog_counts["MAPPED_ACTIVE"],
                "unmapped_candidate_count": catalog_counts["UNMAPPED"],
                "observable_behavior_labels": counts["observable"],
                "excluded_behavior_labels": counts["excluded"],
            },
            "quality": {
                "state_future_leakage_count": state_future_leakage_count,
                "queue_intent_labels": 0,
                "non_null_missing_fields": non_null_missing_fields,
                "causal_reward_rows": 0,
                "full_state_bc_ready": False,
                "offline_rl_ready": False,
                "training_scope": "partial-observation behavior analysis only",
            },
        }
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{base}__",
            suffix=".manifest.json.tmp",
            dir=output_path,
            delete=False,
        ) as handle:
            manifest_temporary = Path(handle.name)
            json.dump(output_manifest, handle, ensure_ascii=False, indent=2)
            handle.write("\n")

        trajectory_temporary.replace(trajectory_output)
        trajectory_temporary = None
        manifest_temporary.replace(manifest_output)
        manifest_temporary = None
    finally:
        if trajectory_temporary is not None:
            trajectory_temporary.unlink(missing_ok=True)
        if manifest_temporary is not None:
            manifest_temporary.unlink(missing_ok=True)

    return DecisionWindowResult(
        start_candidate_count=len(output_records),
        observable_behavior_labels=counts["observable"],
        trajectory=trajectory_output,
        manifest=manifest_output,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build conservative Fury decision windows from trajectory v1."
    )
    parser.add_argument("--trajectory", type=Path, required=True)
    parser.add_argument("--trajectory-manifest", type=Path, required=True)
    parser.add_argument("--readiness-report", type=Path, required=True)
    parser.add_argument("--encounter", required=True)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = build_decision_windows(
            trajectory=args.trajectory,
            trajectory_manifest=args.trajectory_manifest,
            readiness_report=args.readiness_report,
            encounter=args.encounter,
            registry=args.registry,
            output_dir=args.output_dir,
        )
    except DecisionWindowError as error:
        print(f"Fury decision-window ETL failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
