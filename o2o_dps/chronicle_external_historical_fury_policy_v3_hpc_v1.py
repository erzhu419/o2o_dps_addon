"""Bounded six-node A/B diagnostic for the Chronicle Fury V3 feature view.

One worker reads exactly one published Stage-5 partition and that instance's
small official CombatantInfo object.  Both arms use the same V2-filtered
15-action labels and unchanged learner parameters: arm A keeps the frozen V2
contexts, while arm B substitutes the causal V3 prefix/static contexts.  The
reducer never reopens Stage-5 or CombatantInfo inputs.  Validation permits a
next experiment only when top-1, top-3, contextual log loss, and ECE all
improve, while the frozen A top-1/top-3 values are reproduced.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import gzip
import hashlib
import json
import os
from pathlib import Path
import pickle
import tempfile
import time
from typing import Any

from . import chronicle_external_historical_fury_policy_v2 as policy_v2
from . import chronicle_external_historical_fury_policy_v3 as policy_v3
from . import chronicle_external_reconstruction_admission_v1 as admission_v1


JSONMap = dict[str, Any]
SCHEMA = "chronicle_external_historical_fury_policy_v3_hpc/v1"
REVISION = "paired_v2_view_vs_v3_causal_view_external84_v1"
NODES = tuple(f"node{index:03d}" for index in range(1, 7))

EXPECTED_INSTANCE_COUNT = 84
# The old global audit's 867,659 labels include 199,803 Arms labels.  This
# Fury-only paired diagnostic must reproduce the frozen Fury lane's 667,856.
EXPECTED_V2_FURY_LABELS = 667_856
EXPECTED_PAIRED_LABELS = 182_911
EXPECTED_V2_ARMS_LABELS = 199_803
EXPECTED_ARMS_WHITELIST_LABELS = 49_201
EXPECTED_ALL_WARRIOR_LABELS = 867_659
EXPECTED_BASELINE_TOP1 = 0.3189419991
EXPECTED_BASELINE_TOP3 = 0.5775759796
BASELINE_CROSSCHECK_TOLERANCE = 5e-10

RAW_OBJECT_SUBTREE = Path("chronicle_raw/external_api/v1")
DEFAULT_STAGE5_RELATIVE = Path(
    "derived/chronicle_external_team_wave_model/v2/utk_postfix_dev_20260903_noon/manifest.json"
)
DEFAULT_ADMISSION_RELATIVE = Path(
    "derived/chronicle_external_reconstruction_admission/v1/utk_postfix_dev_20260903_noon/manifest.json"
)
DEFAULT_OUTPUT_RELATIVE = Path(
    "behavior_models/chronicle_external_historical_fury_policy/v3/utk_postfix_dev_20260903_noon"
)

WHITELIST_SPELL_IDS = frozenset(
    spell_id for spec in policy_v3.ACTION_ONTOLOGY for spell_id in spec.spell_ids
)


class HistoricalFuryV3HpcError(RuntimeError):
    pass


@dataclass
class _PairedPlayerResult:
    baseline: policy_v2._Aggregate = field(default_factory=policy_v2._Aggregate)
    v3: policy_v2._Aggregate = field(default_factory=policy_v2._Aggregate)
    audit: Counter[str] = field(default_factory=Counter)
    coverage: Counter[str] = field(default_factory=Counter)


def _canonical(value: Any) -> bytes:
    return policy_v2._canonical_bytes(value)


def _file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    payload = _canonical(value) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_json(path_value: str | Path) -> tuple[Path, JSONMap, bytes]:
    path = Path(path_value).expanduser().resolve()
    try:
        payload = path.read_bytes()
        value = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HistoricalFuryV3HpcError(f"cannot read JSON {path}: {error}") from error
    if not isinstance(value, dict) or payload != _canonical(value) + b"\n":
        raise HistoricalFuryV3HpcError(f"not canonical JSON plus LF: {path}")
    return path, value, payload


def _plan_id(plan: Mapping[str, Any]) -> str:
    core = dict(plan)
    observed = core.pop("plan_id", None)
    expected = hashlib.sha256(_canonical(core)).hexdigest()
    if observed != expected:
        raise HistoricalFuryV3HpcError("plan identity differs")
    return expected


def _load_plan(path_value: str | Path) -> tuple[Path, JSONMap]:
    path, plan, _ = _load_json(path_value)
    if plan.get("schema") != SCHEMA or plan.get("revision") != REVISION:
        raise HistoricalFuryV3HpcError("unsupported V3 HPC plan")
    _plan_id(plan)
    return path, plan


def _under(path_value: str | Path, root: Path, label: str) -> Path:
    path = Path(path_value).expanduser().resolve()
    root = root.expanduser().resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise HistoricalFuryV3HpcError(f"{label} must stay under shared root") from error
    return path


def _code_hashes(root: Path) -> JSONMap:
    relative = {
        "policy_v2": "o2o_dps/chronicle_external_historical_fury_policy_v2.py",
        "policy_v3": "o2o_dps/chronicle_external_historical_fury_policy_v3.py",
        "team_wave_model": "o2o_dps/chronicle_external_team_wave_model_v2.py",
        "admission": "o2o_dps/chronicle_external_reconstruction_admission_v1.py",
        "external_api_ingest": "o2o_dps/chronicle_external_api_ingest_v1.py",
        "event_normalizer": "o2o_dps/chronicle_external_event_normalizer_v1.py",
        "combatant_decoder": "o2o_dps/chronicle_combatant_sidecar.py",
        "orchestrator": "o2o_dps/chronicle_external_historical_fury_policy_v3_hpc_v1.py",
    }
    return {key: _file_sha(root / value) for key, value in relative.items()}


def _load_admission_bindings(path_value: str | Path) -> tuple[Path, JSONMap, JSONMap]:
    document, requested = admission_v1.load_admission_manifest(path_value)
    payload = requested.read_bytes()
    content_sha = admission_v1._verify_content_address(
        document, label="admission manifest"
    )
    by_instance: dict[str, Any] = {}
    for raw_entry in document["instances"]:
        instance_id = str(raw_entry["instance_id"])
        evidence = raw_entry["combatant_info_evidence"]
        obj = evidence["object"]
        if (
            evidence.get("status") != "VERIFIED_LOCAL_OFFICIAL_STREAM"
            or evidence.get("identity_conflict_count") != 0
            or evidence.get("unanchored_message_count") != 0
        ):
            raise HistoricalFuryV3HpcError(
                f"CombatantInfo evidence is not causally clean: {instance_id}"
            )
        by_instance[instance_id] = {
            "slug": raw_entry["slug"],
            "relative_path": obj["relative_path"],
            "size_bytes": obj["size_bytes"],
            "sha256": obj["sha256"],
            "message_count": evidence["message_count"],
            "message_with_gear_count": evidence["message_with_gear_count"],
            "message_with_talents_count": evidence["message_with_talents_count"],
        }
    return requested, by_instance, {
        "content_sha256": content_sha,
        "file_sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }


def _lpt_assign(
    tasks: list[JSONMap], nodes: Sequence[str]
) -> tuple[list[JSONMap], JSONMap]:
    if not nodes or len(set(nodes)) != len(nodes):
        raise HistoricalFuryV3HpcError("LPT nodes must be unique and nonempty")
    loads = {node: 0 for node in nodes}
    counts = {node: 0 for node in nodes}
    assignments: dict[int, str] = {}
    for task in sorted(
        tasks,
        key=lambda row: (-int(row["stage5_partition_size_bytes"]), int(row["position"])),
    ):
        node = min(nodes, key=lambda value: (loads[value], counts[value], value))
        position = int(task["position"])
        assignments[position] = node
        loads[node] += int(task["stage5_partition_size_bytes"])
        counts[node] += 1
    assigned = [{**task, "node": assignments[int(task["position"])]} for task in tasks]
    return assigned, {
        node: {"task_count": counts[node], "stage5_compressed_bytes": loads[node]}
        for node in nodes
    }


def make_plan(
    *,
    team_wave_model_manifest: str | Path,
    admission_manifest: str | Path,
    shared_root: str | Path,
    output_directory: str | Path,
    work_directory: str | Path,
    nodes: Sequence[str] = NODES,
    expected_instance_count: int = EXPECTED_INSTANCE_COUNT,
    expected_v2_labels: int = EXPECTED_V2_FURY_LABELS,
    expected_paired_labels: int = EXPECTED_PAIRED_LABELS,
    expected_baseline_top1: float = EXPECTED_BASELINE_TOP1,
    expected_baseline_top3: float = EXPECTED_BASELINE_TOP3,
) -> Path:
    """Bind two small manifests and assign partitions by fixed LPT."""

    shared = Path(shared_root).expanduser().resolve()
    output = _under(output_directory, shared, "output directory")
    work = _under(work_directory, shared, "work directory")
    manifest, manifest_path, stage5_source = policy_v2._load_published_input_shallow(
        team_wave_model_manifest
    )
    admission_path, admission_by_id, admission_source = _load_admission_bindings(
        admission_manifest
    )
    order = policy_v2._array(manifest.get("instance_order"), "instance_order")
    entries = policy_v2._array(manifest.get("instances"), "instances")
    if len(entries) != expected_instance_count or len(order) != expected_instance_count:
        raise HistoricalFuryV3HpcError("Stage5 instance count differs from frozen cohort")
    if set(map(str, order)) != set(admission_by_id):
        raise HistoricalFuryV3HpcError("Stage5 and admission instance sets differ")
    tasks: list[JSONMap] = []
    for position, raw_entry in enumerate(entries):
        entry = policy_v2._mapping(raw_entry, "Stage5 instance")
        instance_id = policy_v2._text(entry.get("instance_id"), "instance_id")
        partition = policy_v2._mapping(entry.get("partition"), "Stage5 partition")
        tasks.append(
            {
                "position": position,
                "instance_id": instance_id,
                "stage5_entry_content_sha256": policy_v2._verify_content_address(
                    entry, "Stage5 instance"
                ),
                "stage5_partition_size_bytes": policy_v2._nonnegative_integer(
                    partition.get("compressed_size_bytes"), "Stage5 partition size"
                ),
                "combatant_info": admission_by_id[instance_id],
            }
        )
    assigned, lpt_load = _lpt_assign(tasks, nodes)
    project_root = Path(__file__).resolve().parents[1]
    core: JSONMap = {
        "schema": SCHEMA,
        "revision": REVISION,
        "status": "PREPARED_NOT_EXECUTED",
        "stage5_manifest": str(manifest_path),
        "stage5_source": stage5_source,
        "admission_manifest": str(admission_path),
        "admission_source": admission_source,
        "shared_root": str(shared),
        "output_directory": str(output),
        "work_directory": str(work),
        "nodes": list(nodes),
        "lpt_load": lpt_load,
        "fixed_parameters": {
            "fold_count": policy_v2.DEFAULT_FOLD_COUNT,
            "split_seed": policy_v2.DEFAULT_SPLIT_SEED,
            "smoothing_alpha": policy_v2.DEFAULT_SMOOTHING_ALPHA,
            "backoff_strength": policy_v2.DEFAULT_BACKOFF_STRENGTH,
            "hyperparameter_search": False,
        },
        "expected_crosscheck": {
            "v2_fury_labels_before_whitelist": expected_v2_labels,
            "paired_fifteen_action_labels": expected_paired_labels,
            "v2_arms_labels_descriptive": EXPECTED_V2_ARMS_LABELS,
            "arms_fifteen_action_labels_descriptive": EXPECTED_ARMS_WHITELIST_LABELS,
            "all_warrior_labels_descriptive": EXPECTED_ALL_WARRIOR_LABELS,
            "baseline_top1": expected_baseline_top1,
            "baseline_top3": expected_baseline_top3,
            "absolute_tolerance": BASELINE_CROSSCHECK_TOLERANCE,
        },
        "implementation_sha256": _code_hashes(project_root),
        "tasks": assigned,
        "comparison_contract": {
            "paired_label_universe": True,
            "same_outer_components_and_folds": True,
            "same_learner_and_fixed_parameters": True,
            "arm_a": "frozen V2 feature view",
            "arm_b": "V3 causal prefix plus static character view",
            "next_step_requires_strict_improvement": [
                "top1_accuracy",
                "top3_accuracy",
                "contextual_log_loss",
                "expected_calibration_error",
            ],
        },
        "runner_or_deployment_authorized": False,
    }
    plan = {**core, "plan_id": hashlib.sha256(_canonical(core)).hexdigest()}
    plan_path = work / "plan.json"
    _write_json(plan_path, plan)
    return plan_path


def _live_code_matches(plan: Mapping[str, Any]) -> None:
    if _code_hashes(Path(__file__).resolve().parents[1]) != plan.get(
        "implementation_sha256"
    ):
        raise HistoricalFuryV3HpcError("implementation differs from plan")


def _task(plan: Mapping[str, Any], instance_id: str) -> Mapping[str, Any]:
    matches = [row for row in plan["tasks"] if row.get("instance_id") == instance_id]
    if len(matches) != 1:
        raise HistoricalFuryV3HpcError(f"instance is not a unique task: {instance_id}")
    return matches[0]


def _task_paths(plan: Mapping[str, Any], position: int) -> tuple[Path, Path]:
    root = Path(str(plan["work_directory"])) / "shards"
    stem = f"{position:03d}"
    return root / f"{stem}.aggregate.pkl", root / f"{stem}.done.json"


def _read_combatant_object(path: Path) -> bytes:
    return path.read_bytes()


def _event_index(label: Mapping[str, Any], order: Sequence[int]) -> int:
    del label
    return int(order[2])


def _paired_player(
    *,
    player: Mapping[str, Any],
    trace: Sequence[Mapping[str, Any]],
    instance_id: str,
    encounter_id: str,
    inventory_index: policy_v3.CharacterInventoryIndex,
) -> _PairedPlayerResult:
    metadata = policy_v2._mapping(player.get("player"), "player metadata")
    focal_guid = policy_v2._text(metadata.get("guid"), "player guid")
    result = _PairedPlayerResult()
    history: list[policy_v3.PrefixAction] = []
    pending_v2: dict[tuple[str, str], int] = {}
    pending_v3: dict[tuple[str, str], int] = {}
    prior_order: tuple[int, ...] | None = None
    for raw_transition in policy_v2._array(
        player.get("prefix_transitions"), "prefix_transitions"
    ):
        transition = policy_v2._mapping(raw_transition, "prefix transition")
        trace_index, order, state, label = policy_v3._validate_transition(
            transition,
            trace=trace,
            focal_guid=focal_guid,
            prior_order=prior_order,
        )
        prior_order = order
        result.audit["transitions_inspected"] += 1
        event_type = policy_v2._text(label.get("event_type"), "event_type")
        spell = policy_v2._mapping(label.get("spell"), "spell")
        classification = policy_v3.classify_action_event(
            event_type=event_type, spell=spell
        )
        identity_v2 = policy_v2._transition_identity(label)
        identity_v3 = (
            policy_v3._action_identity(classification.action_key, label)
            if classification.action_key is not None
            else None
        )
        # Keep the frozen V2 START/GO state machine exact even where V3 gives
        # the same row a different semantic role.  In particular, V2 clears a
        # pending START on FAIL before applying the direct-source filter.
        if event_type == "FAIL":
            pending_v2.pop(identity_v2, None)
        elif event_type not in {"START", "GO"}:
            raise policy_v2.ExternalHistoricalFuryPolicyV2Error(
                "UNSUPPORTED_ACTION_EVENT", f"unsupported action event {event_type}"
            )
        direct = (
            label.get("attribution_kind") == "DIRECT_FRIENDLY_PLAYER"
            and label.get("source_lane") == "FRIENDLY_PLAYER"
        )
        if not direct:
            result.audit["non_direct_player_event_excluded"] += 1
            continue

        timestamp_ms = int(order[1])
        if event_type == "FAIL":
            if identity_v3 is not None:
                pending_v3.pop(identity_v3, None)
            result.audit[f"semantic:{classification.role}"] += 1
            result.audit["failed_attempts_excluded"] += 1
            continue

        wave_elapsed = policy_v2._nonnegative_integer(
            state.get("wave_elapsed_ms"), "wave_elapsed_ms"
        )
        pending_v2 = {
            key: start
            for key, start in pending_v2.items()
            if wave_elapsed - start <= policy_v2.START_GO_PAIR_MAX_MS
        }
        pending_v3 = {
            key: start
            for key, start in pending_v3.items()
            if 0 <= timestamp_ms - start <= policy_v2.START_GO_PAIR_MAX_MS
        }

        v2_paired = False
        if event_type == "GO" and identity_v2 in pending_v2:
            start = pending_v2.pop(identity_v2)
            v2_paired = 0 <= wave_elapsed - start <= policy_v2.START_GO_PAIR_MAX_MS
            if v2_paired:
                result.audit["v2_paired_go_deduplicated"] += 1

        v3_paired = False
        if event_type == "GO" and identity_v3 is not None and identity_v3 in pending_v3:
            start = pending_v3.pop(identity_v3)
            v3_paired = 0 <= timestamp_ms - start <= policy_v2.START_GO_PAIR_MAX_MS
        semantic_role = policy_v3.ROLE_PAIRED_RESULT if v3_paired else classification.role
        result.audit[f"semantic:{semantic_role}"] += 1

        v3_decision = semantic_role in {
            policy_v3.ROLE_CONTROLLABLE_START,
            policy_v3.ROLE_CONTROLLABLE_INSTANT_GO,
        }
        target_role: str | None = None
        target_usable = False
        if v3_decision:
            assert classification.action_key is not None
            assert classification.lane is not None
            target_role, target_usable = policy_v2._target_role(
                label=label, state=state, focal_guid=focal_guid
            )

        if not v2_paired:
            action_key, usable = policy_v2._action_key(
                label=label, state=state, focal_guid=focal_guid
            )
            if not usable:
                result.audit["v2_unsupported_nonvoting_targets_excluded"] += 1
            else:
                result.audit["v2_fury_labels_before_whitelist"] += 1
                if event_type == "START":
                    pending_v2[identity_v2] = wave_elapsed
            spell_id, _ = policy_v3._spell_parts(spell)
            if usable and spell_id in WHITELIST_SPELL_IDS:
                baseline_contexts = policy_v2._contexts_from_prefix(
                    state=state, trace=trace, focal_guid=focal_guid
                )
                inventory, join_outcome = inventory_index.select(
                    instance_ref=instance_id,
                    encounter_id=encounter_id,
                    player_guid=focal_guid,
                    event_index=_event_index(label, order),
                )
                v3_contexts = policy_v3._contexts_with_observable_state(
                    state=state,
                    trace=trace,
                    focal_guid=focal_guid,
                    current_timestamp_ms=timestamp_ms,
                    history=history,
                    inventory=inventory,
                )
                result.baseline.add(baseline_contexts, action_key)
                result.v3.add(v3_contexts, action_key)
                result.audit["paired_fifteen_action_labels"] += 1
                result.coverage["paired_labels"] += 1
                result.coverage[f"inventory_join:{join_outcome}"] += 1
                if inventory is not None:
                    result.coverage["inventory_matched"] += 1
                    result.coverage["gear_observed"] += int(bool(inventory.gear_slots))
                    result.coverage["talents_observed"] += int(
                        inventory.talent_trees is not None
                    )
                result.coverage["prior_controllable_action_observed"] += int(
                    bool(history)
                )
                result.coverage["prior_gcd_action_observed"] += int(
                    any(row.lane == "gcd" for row in history)
                )
                result.coverage["prior_queue_decision_observed"] += int(
                    any(row.lane == "queue" for row in history)
                )
            elif usable:
                result.audit["nonwhitelist_v2_label_excluded"] += 1

        if v3_decision:
            assert classification.action_key is not None
            assert classification.lane is not None
            if event_type == "START":
                assert identity_v3 is not None
                pending_v3[identity_v3] = timestamp_ms
            if target_usable:
                result.audit["strict_controllable_voting_labels"] += 1
            history.append(
                policy_v3.PrefixAction(
                    action_key=classification.action_key,
                    lane=classification.lane,
                    timestamp_ms=timestamp_ms,
                    trace_index=trace_index,
                )
            )
    return result


def _v2_descriptive_label_counts(
    *, player: Mapping[str, Any], trace: Sequence[Mapping[str, Any]]
) -> tuple[int, int]:
    """Count an Arms lane with frozen V2 semantics; never fit it into V3."""

    metadata = policy_v2._mapping(player.get("player"), "player metadata")
    focal_guid = policy_v2._text(metadata.get("guid"), "player guid")
    pending: dict[tuple[str, str], int] = {}
    accepted = 0
    whitelisted = 0
    prior_order: tuple[int, ...] | None = None
    for raw_transition in policy_v2._array(
        player.get("prefix_transitions"), "prefix_transitions"
    ):
        transition = policy_v2._mapping(raw_transition, "prefix transition")
        _trace_index, order, state, label = policy_v3._validate_transition(
            transition,
            trace=trace,
            focal_guid=focal_guid,
            prior_order=prior_order,
        )
        prior_order = order
        event_type = policy_v2._text(label.get("event_type"), "event_type")
        identity = policy_v2._transition_identity(label)
        if event_type == "FAIL":
            pending.pop(identity, None)
            continue
        if (
            label.get("attribution_kind") != "DIRECT_FRIENDLY_PLAYER"
            or label.get("source_lane") != "FRIENDLY_PLAYER"
        ):
            continue
        elapsed = policy_v2._nonnegative_integer(
            state.get("wave_elapsed_ms"), "wave_elapsed_ms"
        )
        pending = {
            key: start
            for key, start in pending.items()
            if elapsed - start <= policy_v2.START_GO_PAIR_MAX_MS
        }
        if event_type == "GO" and identity in pending:
            start = pending.pop(identity)
            if 0 <= elapsed - start <= policy_v2.START_GO_PAIR_MAX_MS:
                continue
        _action_key, usable = policy_v2._action_key(
            label=label, state=state, focal_guid=focal_guid
        )
        if usable:
            accepted += 1
            spell = policy_v2._mapping(label.get("spell"), "spell")
            spell_id, _ = policy_v3._spell_parts(spell)
            whitelisted += int(spell_id in WHITELIST_SPELL_IDS)
        if usable and event_type == "START":
            pending[identity] = elapsed
    return accepted, whitelisted


def _scan_partition(
    *,
    manifest: Mapping[str, Any],
    manifest_path: Path,
    task: Mapping[str, Any],
    inventory_index: policy_v3.CharacterInventoryIndex,
) -> tuple[dict[str, policy_v2._Aggregate], dict[str, policy_v2._Aggregate], Counter[str], Counter[str]]:
    instance_id = str(task["instance_id"])
    position = int(task["position"])
    entry = policy_v2._mapping(manifest["instances"][position], "Stage5 instance")
    if (
        entry.get("instance_id") != instance_id
        or policy_v2._verify_content_address(entry, "Stage5 instance")
        != task["stage5_entry_content_sha256"]
    ):
        raise HistoricalFuryV3HpcError("Stage5 task entry differs from plan")
    partition = policy_v2._mapping(entry.get("partition"), "Stage5 partition")
    path = policy_v2._resolve_partition(
        manifest_path,
        partition.get("path"),
        policy_v2._data_root(manifest_path),
    )
    if (
        path.stat().st_size != partition.get("compressed_size_bytes")
        or _file_sha(path) != partition.get("compressed_file_sha256")
    ):
        raise HistoricalFuryV3HpcError("selected Stage5 partition identity differs")
    node_index, _ = policy_v2._outer_component_index(manifest)
    baseline_by_outer: dict[str, policy_v2._Aggregate] = defaultdict(
        policy_v2._Aggregate
    )
    v3_by_outer: dict[str, policy_v2._Aggregate] = defaultdict(policy_v2._Aggregate)
    audit: Counter[str] = Counter()
    coverage: Counter[str] = Counter()
    with gzip.open(path, "rb") as handle:
        for line_number, raw_line in enumerate(handle, 1):
            if not raw_line.strip():
                continue
            try:
                wave = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise HistoricalFuryV3HpcError(
                    f"invalid Stage5 JSON {path}:{line_number}: {error}"
                ) from error
            if raw_line != _canonical(wave) + b"\n":
                raise HistoricalFuryV3HpcError(
                    f"noncanonical Stage5 row {path}:{line_number}"
                )
            wave_identity = policy_v2._mapping(wave.get("wave"), "wave identity")
            if wave_identity.get("instance_id") != instance_id:
                raise HistoricalFuryV3HpcError("Stage5 wave crossed instance partition")
            encounter_id = policy_v2._text(
                wave_identity.get("encounter_id"), "encounter_id"
            )
            trace = [
                policy_v2._mapping(row, "trace row")
                for row in policy_v2._array(wave.get("exact_trace"), "exact_trace")
            ]
            for raw_player in policy_v2._array(wave.get("players"), "players"):
                player = policy_v2._mapping(raw_player, "wave player")
                metadata = policy_v2._mapping(player.get("player"), "player metadata")
                if str(metadata.get("class") or "").upper() != "WARRIOR":
                    continue
                spec = policy_v2._mapping(
                    player.get("warrior_spec_lane"), "warrior_spec_lane"
                )
                eligibility = policy_v2._mapping(
                    player.get("eligibility_observation"), "eligibility_observation"
                )
                lane = spec.get("partition_key")
                if (
                    lane == policy_v2.ARMS_LANE
                    and eligibility.get("arms_diagnostic_candidate_filter_passed")
                    is True
                ):
                    arms_all, arms_whitelist = _v2_descriptive_label_counts(
                        player=player, trace=trace
                    )
                    audit["arms_v2_labels_descriptive"] += arms_all
                    audit["arms_fifteen_action_labels_descriptive"] += arms_whitelist
                    continue
                if (
                    lane != policy_v2.FURY_LANE
                    or eligibility.get("historical_fury_candidate_filter_passed") is not True
                ):
                    continue
                instance_node, player_node, component = (
                    policy_v2._episode_nodes_and_outer_component(player, node_index)
                )
                paired = _paired_player(
                    player=player,
                    trace=trace,
                    instance_id=instance_id,
                    encounter_id=encounter_id,
                    inventory_index=inventory_index,
                )
                paired.baseline.observe_episode(
                    instance_node=instance_node, player_node=player_node
                )
                paired.v3.observe_episode(
                    instance_node=instance_node, player_node=player_node
                )
                baseline_by_outer[component].merge(paired.baseline)
                v3_by_outer[component].merge(paired.v3)
                audit.update(paired.audit)
                coverage.update(paired.coverage)
    return dict(baseline_by_outer), dict(v3_by_outer), audit, coverage


def _resource_usage(started_wall: float, started_cpu: float) -> JSONMap:
    result: JSONMap = {
        "wall_seconds": time.perf_counter() - started_wall,
        "cpu_seconds": time.process_time() - started_cpu,
        "gomaxprocs": os.environ.get("GOMAXPROCS"),
    }
    try:
        import resource

        result["max_rss_kib"] = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except ImportError:
        result["max_rss_kib"] = None
    return result


def run_worker(
    *, plan_path: str | Path, instance_id: str, node: str
) -> JSONMap:
    started_wall = time.perf_counter()
    started_cpu = time.process_time()
    if os.environ.get("GOMAXPROCS") != "1":
        raise HistoricalFuryV3HpcError("worker requires GOMAXPROCS=1")
    _, plan = _load_plan(plan_path)
    _live_code_matches(plan)
    task = _task(plan, instance_id)
    if task.get("node") != node:
        raise HistoricalFuryV3HpcError("worker node differs from LPT assignment")
    aggregate_path, receipt_path = _task_paths(plan, int(task["position"]))
    if aggregate_path.is_file() and receipt_path.is_file():
        receipt = _load_json(receipt_path)[1]
        if (
            receipt.get("plan_id") != plan["plan_id"]
            or receipt.get("instance_id") != instance_id
        ):
            raise HistoricalFuryV3HpcError("existing worker receipt differs")
        return {**receipt, "status": "RESUMED"}

    manifest, manifest_path, stage5_source = policy_v2._load_published_input_shallow(
        plan["stage5_manifest"]
    )
    if stage5_source != plan["stage5_source"]:
        raise HistoricalFuryV3HpcError("Stage5 manifest changed after planning")
    info = policy_v2._mapping(task.get("combatant_info"), "combatant_info")
    data_root = policy_v2._data_root(manifest_path)
    object_path = data_root / RAW_OBJECT_SUBTREE / str(info["relative_path"])
    compressed = _read_combatant_object(object_path)
    if (
        len(compressed) != info.get("size_bytes")
        or hashlib.sha256(compressed).hexdigest() != info.get("sha256")
    ):
        raise HistoricalFuryV3HpcError("CombatantInfo object identity differs")
    inventory_index = policy_v3.inventory_index_from_compressed_stream(
        compressed, instance_ref=instance_id, slug=str(info["slug"])
    )
    baseline, v3, audit, coverage = _scan_partition(
        manifest=manifest,
        manifest_path=manifest_path,
        task=task,
        inventory_index=inventory_index,
    )
    aggregate_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = aggregate_path.with_name(f".{aggregate_path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as handle:
            pickle.dump(
                {
                    "plan_id": plan["plan_id"],
                    "instance_id": instance_id,
                    "baseline_by_outer": baseline,
                    "v3_by_outer": v3,
                    "audit": audit,
                    "coverage": coverage,
                },
                handle,
                protocol=pickle.HIGHEST_PROTOCOL,
            )
        temporary.replace(aggregate_path)
    finally:
        temporary.unlink(missing_ok=True)
    receipt: JSONMap = {
        "schema": SCHEMA + "/worker_receipt",
        "revision": REVISION,
        "status": "COMPLETE",
        "plan_id": plan["plan_id"],
        "position": task["position"],
        "instance_id": instance_id,
        "node": node,
        "stage5_partition_bytes": task["stage5_partition_size_bytes"],
        "combatant_info_bytes": info["size_bytes"],
        "v2_fury_labels_before_whitelist": audit.get(
            "v2_fury_labels_before_whitelist", 0
        ),
        "paired_fifteen_action_labels": audit.get(
            "paired_fifteen_action_labels", 0
        ),
        "strict_controllable_voting_labels": audit.get(
            "strict_controllable_voting_labels", 0
        ),
        "resource_usage": _resource_usage(started_wall, started_cpu),
    }
    _write_json(receipt_path, receipt)
    return receipt


def status(*, plan_path: str | Path) -> JSONMap:
    _, plan = _load_plan(plan_path)
    by_node: Counter[str] = Counter()
    complete = 0
    for task in plan["tasks"]:
        aggregate, receipt = _task_paths(plan, int(task["position"]))
        if aggregate.is_file() and receipt.is_file():
            complete += 1
            by_node[str(task["node"])] += 1
    return {
        "status": "COMPLETE" if complete == len(plan["tasks"]) else "INCOMPLETE",
        "complete": complete,
        "total": len(plan["tasks"]),
        "complete_by_node": dict(sorted(by_node.items())),
    }


def _merge_component_maps(
    destination: dict[str, policy_v2._Aggregate],
    source: Mapping[str, policy_v2._Aggregate],
) -> None:
    for component, aggregate in source.items():
        destination[component].merge(aggregate)


def _sum_decisions(components: Mapping[str, policy_v2._Aggregate]) -> int:
    return sum(aggregate.decision_count for aggregate in components.values())


def _coverage_report(counts: Mapping[str, int]) -> JSONMap:
    denominator = int(counts.get("paired_labels", 0))
    return {
        "counts": dict(sorted((key, int(value)) for key, value in counts.items())),
        "rates": {
            key: int(value) / denominator
            for key, value in sorted(counts.items())
            if key != "paired_labels" and denominator
        },
    }


def reduce(*, plan_path: str | Path) -> JSONMap:
    """Merge worker sufficient statistics without reopening either raw input."""

    started_wall = time.perf_counter()
    started_cpu = time.process_time()
    _, plan = _load_plan(plan_path)
    _live_code_matches(plan)
    manifest, _manifest_path, stage5_source = policy_v2._load_published_input_shallow(
        plan["stage5_manifest"]
    )
    if stage5_source != plan["stage5_source"]:
        raise HistoricalFuryV3HpcError("Stage5 manifest changed before reduce")
    # Reading the small admission publication is enough; no object is opened.
    admission_path, _bindings, admission_source = _load_admission_bindings(
        plan["admission_manifest"]
    )
    if str(admission_path) != plan["admission_manifest"] or admission_source != plan[
        "admission_source"
    ]:
        raise HistoricalFuryV3HpcError("admission manifest changed before reduce")

    baseline: dict[str, policy_v2._Aggregate] = defaultdict(policy_v2._Aggregate)
    v3: dict[str, policy_v2._Aggregate] = defaultdict(policy_v2._Aggregate)
    audit: Counter[str] = Counter()
    coverage: Counter[str] = Counter()
    worker_resources: list[JSONMap] = []
    for task in plan["tasks"]:
        aggregate_path, receipt_path = _task_paths(plan, int(task["position"]))
        if not aggregate_path.is_file() or not receipt_path.is_file():
            raise HistoricalFuryV3HpcError(
                f"worker output incomplete: {task['instance_id']}"
            )
        receipt = _load_json(receipt_path)[1]
        if (
            receipt.get("status") != "COMPLETE"
            or receipt.get("plan_id") != plan["plan_id"]
            or receipt.get("instance_id") != task["instance_id"]
            or receipt.get("node") != task["node"]
        ):
            raise HistoricalFuryV3HpcError("worker receipt differs from plan")
        try:
            with aggregate_path.open("rb") as handle:
                worker = pickle.load(handle)
        except (OSError, EOFError, pickle.UnpicklingError) as error:
            raise HistoricalFuryV3HpcError(
                f"cannot read worker aggregate: {aggregate_path}"
            ) from error
        if (
            worker.get("plan_id") != plan["plan_id"]
            or worker.get("instance_id") != task["instance_id"]
        ):
            raise HistoricalFuryV3HpcError("worker aggregate differs from plan")
        _merge_component_maps(baseline, worker["baseline_by_outer"])
        _merge_component_maps(v3, worker["v3_by_outer"])
        audit.update(worker["audit"])
        coverage.update(worker["coverage"])
        worker_resources.append(
            {
                "instance_id": task["instance_id"],
                "node": task["node"],
                **receipt["resource_usage"],
            }
        )
    if _sum_decisions(baseline) != _sum_decisions(v3):
        raise HistoricalFuryV3HpcError("paired A/B label counts differ")
    if set(baseline) != set(v3):
        raise HistoricalFuryV3HpcError("paired A/B component sets differ")
    parameters = plan["fixed_parameters"]
    evaluate = lambda values: policy_v2._evaluate_component_split(
        values,
        fold_count=int(parameters["fold_count"]),
        split_seed=int(parameters["split_seed"]),
        alpha=float(parameters["smoothing_alpha"]),
        backoff_strength=float(parameters["backoff_strength"]),
    )
    baseline_evaluation = evaluate(baseline)
    v3_evaluation = evaluate(v3)
    if baseline_evaluation["component_to_fold"] != v3_evaluation["component_to_fold"]:
        raise HistoricalFuryV3HpcError("paired A/B component folds differ")
    reduced: JSONMap = {
        "schema": SCHEMA + "/reduction",
        "revision": REVISION,
        "status": "REDUCED_AWAITING_VALIDATION",
        "plan_id": plan["plan_id"],
        "source": {
            "stage5_manifest_content_sha256": plan["stage5_source"][
                "manifest_content_sha256"
            ],
            "admission_manifest_content_sha256": plan["admission_source"][
                "content_sha256"
            ],
        },
        "accounting": {
            "worker_count": len(plan["tasks"]),
            "outer_component_count": len(baseline),
            "baseline_decision_count": _sum_decisions(baseline),
            "v3_decision_count": _sum_decisions(v3),
            "action_audit": dict(sorted(audit.items())),
            "denominators": {
                "fury": {
                    "v2_pre_whitelist": int(
                        audit.get("v2_fury_labels_before_whitelist", 0)
                    ),
                    "paired_fifteen_action": _sum_decisions(baseline),
                },
                "arms_descriptive_only": {
                    "v2_pre_whitelist": int(audit.get("arms_v2_labels_descriptive", 0)),
                    "fifteen_action": int(
                        audit.get("arms_fifteen_action_labels_descriptive", 0)
                    ),
                },
                "all_warrior_v2_pre_whitelist": int(
                    audit.get("v2_fury_labels_before_whitelist", 0)
                )
                + int(audit.get("arms_v2_labels_descriptive", 0)),
            },
            "feature_coverage": _coverage_report(coverage),
        },
        "paired_evaluation": {
            "arm_a_frozen_v2_feature_view": baseline_evaluation,
            "arm_b_v3_causal_feature_view": v3_evaluation,
        },
        "resource_usage": {
            "reduce": _resource_usage(started_wall, started_cpu),
            "workers": worker_resources,
        },
        "missing_dynamic_state": list(policy_v3.MISSING_DYNAMIC_STATE),
        "runner_or_deployment_authorized": False,
    }
    output = Path(str(plan["output_directory"]))
    output.mkdir(parents=True, exist_ok=True)
    _write_json(output / "reduction.json", reduced)
    _write_json(Path(str(plan["work_directory"])) / "reduce.json", reduced)
    return reduced


def _metrics(document: Mapping[str, Any], arm: str) -> Mapping[str, Any]:
    paired = policy_v2._mapping(document.get("paired_evaluation"), "paired_evaluation")
    evaluation = policy_v2._mapping(paired.get(arm), arm)
    return policy_v2._mapping(evaluation.get("metrics"), f"{arm}.metrics")


def validate(*, plan_path: str | Path, reduction_path: str | Path | None = None) -> JSONMap:
    _, plan = _load_plan(plan_path)
    _live_code_matches(plan)
    source = (
        Path(reduction_path)
        if reduction_path is not None
        else Path(str(plan["output_directory"])) / "reduction.json"
    )
    _, reduction, _ = _load_json(source)
    if (
        reduction.get("schema") != SCHEMA + "/reduction"
        or reduction.get("revision") != REVISION
        or reduction.get("plan_id") != plan["plan_id"]
        or reduction.get("status") != "REDUCED_AWAITING_VALIDATION"
    ):
        raise HistoricalFuryV3HpcError("reduction does not belong to this plan")
    accounting = policy_v2._mapping(reduction.get("accounting"), "accounting")
    audit = policy_v2._mapping(accounting.get("action_audit"), "action_audit")
    expected = policy_v2._mapping(plan.get("expected_crosscheck"), "expected_crosscheck")
    a = _metrics(reduction, "arm_a_frozen_v2_feature_view")
    b = _metrics(reduction, "arm_b_v3_causal_feature_view")
    gates = {
        "all_84_workers_present": accounting.get("worker_count") == len(plan["tasks"]),
        "v2_label_count_reproduced": audit.get("v2_fury_labels_before_whitelist")
        == expected["v2_fury_labels_before_whitelist"],
        "arms_label_count_reproduced_descriptive": audit.get(
            "arms_v2_labels_descriptive"
        )
        == expected["v2_arms_labels_descriptive"],
        "arms_whitelist_count_reproduced_descriptive": audit.get(
            "arms_fifteen_action_labels_descriptive"
        )
        == expected["arms_fifteen_action_labels_descriptive"],
        "all_warrior_count_reproduced_descriptive": (
            int(audit.get("v2_fury_labels_before_whitelist", 0))
            + int(audit.get("arms_v2_labels_descriptive", 0))
        )
        == expected["all_warrior_labels_descriptive"],
        "paired_label_count_reproduced": accounting.get("baseline_decision_count")
        == expected["paired_fifteen_action_labels"],
        "paired_arm_counts_equal": accounting.get("baseline_decision_count")
        == accounting.get("v3_decision_count"),
        "strict_ontology_matches_paired_universe": audit.get(
            "strict_controllable_voting_labels"
        )
        == accounting.get("baseline_decision_count"),
        "baseline_top1_reproduced": abs(
            float(a["top1_accuracy"]) - float(expected["baseline_top1"])
        )
        <= float(expected["absolute_tolerance"]),
        "baseline_top3_reproduced": abs(
            float(a["top3_accuracy"]) - float(expected["baseline_top3"])
        )
        <= float(expected["absolute_tolerance"]),
        "same_heldout_count": a.get("heldout_decision_count")
        == b.get("heldout_decision_count"),
    }
    improvements = {
        "top1_accuracy": float(b["top1_accuracy"]) > float(a["top1_accuracy"]),
        "top3_accuracy": float(b["top3_accuracy"]) > float(a["top3_accuracy"]),
        "contextual_log_loss": float(b["contextual_log_loss"])
        < float(a["contextual_log_loss"]),
        "expected_calibration_error": float(b["expected_calibration_error"])
        < float(a["expected_calibration_error"]),
    }
    integrity_pass = all(gates.values())
    baseline_metrics_complete = all(
        value
        for key, value in gates.items()
        if key != "strict_ontology_matches_paired_universe"
    )
    joint_improvement = all(improvements.values())
    next_step_allowed = integrity_pass and joint_improvement
    if next_step_allowed:
        validation_status = "PASS_FOUR_METRIC_JOINT_IMPROVEMENT_DIAGNOSTIC"
    elif not baseline_metrics_complete:
        validation_status = "BASELINE_METRICS_INCOMPLETE"
    elif not gates["strict_ontology_matches_paired_universe"]:
        validation_status = "ONTOLOGY_UNIVERSE_MISMATCH_NO_NEXT_HPC"
    else:
        validation_status = "RETAIN_NEGATIVE_NO_NEXT_HPC"
    validation: JSONMap = {
        "schema": SCHEMA + "/validation",
        "revision": REVISION,
        "plan_id": plan["plan_id"],
        "status": validation_status,
        "integrity_gates": gates,
        "baseline_metrics_complete": baseline_metrics_complete,
        "four_metric_strict_improvement": improvements,
        "metrics": {
            "arm_a": dict(a),
            "arm_b": dict(b),
            "delta_b_minus_a": {
                "top1_accuracy": float(b["top1_accuracy"]) - float(a["top1_accuracy"]),
                "top3_accuracy": float(b["top3_accuracy"]) - float(a["top3_accuracy"]),
                "contextual_log_loss": float(b["contextual_log_loss"])
                - float(a["contextual_log_loss"]),
                "expected_calibration_error": float(b["expected_calibration_error"])
                - float(a["expected_calibration_error"]),
            },
        },
        "next_step_allowed": next_step_allowed,
        "runner_or_deployment_authorized": False,
        "negative_result_retained": not next_step_allowed,
    }
    _write_json(Path(str(plan["output_directory"])) / "validation.json", validation)
    return validation


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--shared-root", type=Path, required=True)
    plan.add_argument("--team-wave-model-manifest", type=Path)
    plan.add_argument("--admission-manifest", type=Path)
    plan.add_argument("--output-directory", type=Path)
    plan.add_argument("--work-directory", type=Path)
    worker = commands.add_parser("worker")
    worker.add_argument("--plan", type=Path, required=True)
    worker.add_argument("--instance-id", required=True)
    worker.add_argument("--node", choices=NODES, required=True)
    status_parser = commands.add_parser("status")
    status_parser.add_argument("--plan", type=Path, required=True)
    reduce_parser = commands.add_parser("reduce")
    reduce_parser.add_argument("--plan", type=Path, required=True)
    validate_parser = commands.add_parser("validate")
    validate_parser.add_argument("--plan", type=Path, required=True)
    validate_parser.add_argument("--reduction", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "plan":
        shared = args.shared_root.expanduser().resolve()
        output = args.output_directory or shared / "offline_data" / DEFAULT_OUTPUT_RELATIVE
        work = args.work_directory or output / ".hpc"
        value: Any = {
            "status": "PREPARED_NOT_EXECUTED",
            "plan_path": str(
                make_plan(
                    team_wave_model_manifest=args.team_wave_model_manifest
                    or shared / "offline_data" / DEFAULT_STAGE5_RELATIVE,
                    admission_manifest=args.admission_manifest
                    or shared / "offline_data" / DEFAULT_ADMISSION_RELATIVE,
                    shared_root=shared,
                    output_directory=output,
                    work_directory=work,
                )
            ),
        }
    elif args.command == "worker":
        value = run_worker(
            plan_path=args.plan, instance_id=args.instance_id, node=args.node
        )
    elif args.command == "status":
        value = status(plan_path=args.plan)
    elif args.command == "reduce":
        value = reduce(plan_path=args.plan)
    else:
        value = validate(plan_path=args.plan, reduction_path=args.reduction)
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
