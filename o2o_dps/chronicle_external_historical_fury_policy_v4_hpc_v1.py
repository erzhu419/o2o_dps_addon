"""Six-node paired A/B/C diagnostic for historical Fury policy V4.

Each worker opens exactly one published Stage5 partition.  The three arms use
the same V3 strict controllable labels and component folds.  Arm C is
preregistered to degenerate to B on the current Stage5 schema because no
dynamic-state envelope is present; that expectation is an integrity check and
does not weaken the frozen four-metric C-versus-A gate.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import pickle
import tempfile
import time
from typing import Any

from . import chronicle_external_historical_fury_policy_v2 as policy_v2
from . import chronicle_external_historical_fury_policy_v3 as policy_v3
from . import chronicle_external_historical_fury_policy_v3_hpc_v1 as hpc_v3
from . import chronicle_external_historical_fury_policy_v4 as policy_v4


JSONMap = dict[str, Any]
SCHEMA = "chronicle_external_historical_fury_policy_v4_hpc/v1"
REVISION = "paired_abc_low_card_prefix_external84_v1"
NODES = tuple(f"node{index:03d}" for index in range(1, 7))
EXPECTED_INSTANCE_COUNT = 84
EXPECTED_STRICT_FURY_LABELS = 182_481
EXPECTED_FURY_PRE_WHITELIST_REFERENCE = 667_856
EXPECTED_ALL_WARRIOR_REFERENCE = 867_659

DEFAULT_STAGE5_RELATIVE = hpc_v3.DEFAULT_STAGE5_RELATIVE
DEFAULT_ADMISSION_RELATIVE = hpc_v3.DEFAULT_ADMISSION_RELATIVE
DEFAULT_OUTPUT_RELATIVE = Path(
    "behavior_models/chronicle_external_historical_fury_policy/v4/"
    "utk_postfix_dev_20260903_noon"
)


class HistoricalFuryV4HpcError(RuntimeError):
    pass


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
        raise HistoricalFuryV4HpcError(f"cannot read JSON {path}: {error}") from error
    if not isinstance(value, dict) or payload != _canonical(value) + b"\n":
        raise HistoricalFuryV4HpcError(f"not canonical JSON plus LF: {path}")
    return path, value, payload


def _plan_id(plan: Mapping[str, Any]) -> str:
    core = dict(plan)
    observed = core.pop("plan_id", None)
    expected = hashlib.sha256(_canonical(core)).hexdigest()
    if observed != expected:
        raise HistoricalFuryV4HpcError("plan identity differs")
    return expected


def _load_plan(path_value: str | Path) -> tuple[Path, JSONMap]:
    path, plan, _ = _load_json(path_value)
    if plan.get("schema") != SCHEMA or plan.get("revision") != REVISION:
        raise HistoricalFuryV4HpcError("unsupported V4 HPC plan")
    _plan_id(plan)
    return path, plan


def _under(path_value: str | Path, root: Path, label: str) -> Path:
    path = Path(path_value).expanduser().resolve()
    root = root.expanduser().resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise HistoricalFuryV4HpcError(f"{label} must stay under shared root") from error
    return path


def _code_hashes(root: Path) -> JSONMap:
    relative = {
        "policy_v2": "o2o_dps/chronicle_external_historical_fury_policy_v2.py",
        "policy_v3": "o2o_dps/chronicle_external_historical_fury_policy_v3.py",
        "policy_v4": "o2o_dps/chronicle_external_historical_fury_policy_v4.py",
        "v3_hpc_helpers": "o2o_dps/chronicle_external_historical_fury_policy_v3_hpc_v1.py",
        "team_wave_model": "o2o_dps/chronicle_external_team_wave_model_v2.py",
        "admission": "o2o_dps/chronicle_external_reconstruction_admission_v1.py",
        "ablation_plan": "configs/evaluation/chronicle_external_historical_fury_policy_v4_ablation.json",
        "orchestrator": "o2o_dps/chronicle_external_historical_fury_policy_v4_hpc_v1.py",
    }
    return {key: _file_sha(root / value) for key, value in relative.items()}


def make_plan(
    *,
    team_wave_model_manifest: str | Path,
    admission_manifest: str | Path,
    shared_root: str | Path,
    output_directory: str | Path,
    work_directory: str | Path,
    nodes: Sequence[str] = NODES,
    expected_instance_count: int = EXPECTED_INSTANCE_COUNT,
    expected_strict_labels: int = EXPECTED_STRICT_FURY_LABELS,
) -> Path:
    """Bind small publications and make one deterministic LPT task per instance."""

    shared = Path(shared_root).expanduser().resolve()
    output = _under(output_directory, shared, "output directory")
    work = _under(work_directory, shared, "work directory")
    manifest, manifest_path, stage5_source = policy_v2._load_published_input_shallow(
        team_wave_model_manifest
    )
    admission_path, admission_by_id, admission_source = hpc_v3._load_admission_bindings(
        admission_manifest
    )
    order = policy_v2._array(manifest.get("instance_order"), "instance_order")
    entries = policy_v2._array(manifest.get("instances"), "instances")
    if len(entries) != expected_instance_count or len(order) != expected_instance_count:
        raise HistoricalFuryV4HpcError("Stage5 instance count differs from frozen cohort")
    if set(map(str, order)) != set(admission_by_id):
        raise HistoricalFuryV4HpcError("Stage5 and admission instance sets differ")
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
                "combatant_info_message_count": admission_by_id[instance_id][
                    "message_count"
                ],
            }
        )
    assigned, lpt_load = hpc_v3._lpt_assign(tasks, nodes)
    project_root = Path(__file__).resolve().parents[1]
    ablation = policy_v4.load_fixed_ablation_plan(
        project_root
        / "configs"
        / "evaluation"
        / "chronicle_external_historical_fury_policy_v4_ablation.json"
    )
    if ablation["execution"]["current_stage5_dynamic_state_envelope_available"]:
        raise HistoricalFuryV4HpcError("frozen V4 plan no longer expects C to equal B")
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
            "strict_fury_labels": expected_strict_labels,
            "fury_pre_whitelist_reference": EXPECTED_FURY_PRE_WHITELIST_REFERENCE,
            "all_warrior_reference": EXPECTED_ALL_WARRIOR_REFERENCE,
            "dynamic_atoms": 0,
            "arm_c_exactly_equals_arm_b": True,
        },
        "implementation_sha256": _code_hashes(project_root),
        "tasks": assigned,
        "comparison_contract": {
            "arms": list(policy_v4.ARMS),
            "paired_label_universe": "V3 strict controllable Fury decisions",
            "same_outer_components_and_folds": True,
            "same_learner_alpha_and_backoff": True,
            "arm_c_preregistered_expected_to_degenerate_to_b": True,
            "primary_gate": "C versus A jointly improves top1 top3 logloss ECE",
        },
        "runner_or_deployment_authorized": False,
    }
    plan = {**core, "plan_id": hashlib.sha256(_canonical(core)).hexdigest()}
    path = work / "plan.json"
    if path.exists():
        _, existing, _ = _load_json(path)
        if existing != plan:
            raise HistoricalFuryV4HpcError("existing V4 plan differs")
        return path
    _write_json(path, plan)
    return path


def _live_code_matches(plan: Mapping[str, Any]) -> None:
    if _code_hashes(Path(__file__).resolve().parents[1]) != plan.get(
        "implementation_sha256"
    ):
        raise HistoricalFuryV4HpcError("implementation differs from plan")


def _task(plan: Mapping[str, Any], instance_id: str) -> Mapping[str, Any]:
    matches = [row for row in plan["tasks"] if row.get("instance_id") == instance_id]
    if len(matches) != 1:
        raise HistoricalFuryV4HpcError(f"instance is not a unique task: {instance_id}")
    return matches[0]


def _task_paths(plan: Mapping[str, Any], position: int) -> tuple[Path, Path]:
    root = Path(str(plan["work_directory"])) / "shards"
    stem = f"{position:03d}"
    return root / f"{stem}.aggregate.pkl", root / f"{stem}.done.json"


def _merge_component_maps(
    destination: dict[str, policy_v4.AdditiveAggregate],
    source: Mapping[str, policy_v4.AdditiveAggregate],
) -> None:
    for component, aggregate in source.items():
        destination.setdefault(component, policy_v4.AdditiveAggregate()).merge(
            aggregate
        )


def _scan_partition(
    *,
    manifest: Mapping[str, Any],
    manifest_path: Path,
    task: Mapping[str, Any],
) -> tuple[dict[str, dict[str, policy_v4.AdditiveAggregate]], Counter[str]]:
    instance_id = str(task["instance_id"])
    position = int(task["position"])
    entry = policy_v2._mapping(manifest["instances"][position], "Stage5 instance")
    if (
        entry.get("instance_id") != instance_id
        or policy_v2._verify_content_address(entry, "Stage5 instance")
        != task["stage5_entry_content_sha256"]
    ):
        raise HistoricalFuryV4HpcError("Stage5 task entry differs from plan")
    partition = policy_v2._mapping(entry.get("partition"), "Stage5 partition")
    path = policy_v2._resolve_partition(
        manifest_path,
        partition.get("path"),
        policy_v2._data_root(manifest_path),
    )
    if path.stat().st_size != partition.get("compressed_size_bytes"):
        raise HistoricalFuryV4HpcError("selected Stage5 partition size differs")
    node_index, _ = policy_v2._outer_component_index(manifest)
    by_arm: dict[str, dict[str, policy_v4.AdditiveAggregate]] = {
        arm: {} for arm in policy_v4.ARMS
    }
    audit: Counter[str] = Counter()
    with gzip.open(path, "rb") as handle:
        for line_number, raw_line in enumerate(handle, 1):
            if not raw_line.strip():
                continue
            try:
                wave = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise HistoricalFuryV4HpcError(
                    f"invalid Stage5 JSON {path}:{line_number}: {error}"
                ) from error
            if raw_line != _canonical(wave) + b"\n":
                raise HistoricalFuryV4HpcError(
                    f"noncanonical Stage5 row {path}:{line_number}"
                )
            identity = policy_v2._mapping(wave.get("wave"), "wave identity")
            if identity.get("instance_id") != instance_id:
                raise HistoricalFuryV4HpcError("Stage5 wave crossed instance partition")
            encounter_id = policy_v2._text(identity.get("encounter_id"), "encounter_id")
            trace = [
                policy_v2._mapping(row, "trace row")
                for row in policy_v2._array(wave.get("exact_trace"), "exact_trace")
            ]
            audit["waves_inspected"] += 1
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
                if (
                    spec.get("partition_key") != policy_v2.FURY_LANE
                    or eligibility.get("historical_fury_candidate_filter_passed")
                    is not True
                ):
                    continue
                instance_node, player_node, component = (
                    policy_v2._episode_nodes_and_outer_component(player, node_index)
                )
                del instance_node, player_node
                diagnostic = policy_v4.process_player_transitions(
                    player=player,
                    trace=trace,
                    instance_ref=instance_id,
                    encounter_id=encounter_id,
                )
                audit["fury_player_waves"] += 1
                audit.update(diagnostic.audit)
                for decision in diagnostic.decisions:
                    if not decision.voting_usable:
                        continue
                    for arm in policy_v4.ARMS:
                        aggregate = by_arm[arm].setdefault(
                            component, policy_v4.AdditiveAggregate()
                        )
                        aggregate.add(
                            decision.feature_view,
                            decision.action_label,
                            arm=arm,
                        )
                    audit["strict_paired_labels"] += 1
                    audit["dynamic_observed_atom_count"] += len(
                        decision.feature_view.dynamic.atoms
                    )
                    audit["dynamic_missing_field_count"] += len(
                        decision.feature_view.dynamic.missing_fields
                    )
                    audit[
                        f"action:{decision.action_key}"
                    ] += 1
    return by_arm, audit


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


def run_worker(*, plan_path: str | Path, instance_id: str, node: str) -> JSONMap:
    started_wall = time.perf_counter()
    started_cpu = time.process_time()
    if os.environ.get("GOMAXPROCS") != "1":
        raise HistoricalFuryV4HpcError("worker requires GOMAXPROCS=1")
    _, plan = _load_plan(plan_path)
    _live_code_matches(plan)
    task = _task(plan, instance_id)
    if task.get("node") != node:
        raise HistoricalFuryV4HpcError("worker node differs from LPT assignment")
    aggregate_path, receipt_path = _task_paths(plan, int(task["position"]))
    if aggregate_path.is_file() and receipt_path.is_file():
        receipt = _load_json(receipt_path)[1]
        if (
            receipt.get("plan_id") != plan["plan_id"]
            or receipt.get("instance_id") != instance_id
        ):
            raise HistoricalFuryV4HpcError("existing worker receipt differs")
        return {**receipt, "status": "RESUMED"}
    manifest, manifest_path, stage5_source = policy_v2._load_published_input_shallow(
        plan["stage5_manifest"]
    )
    if stage5_source != plan["stage5_source"]:
        raise HistoricalFuryV4HpcError("Stage5 manifest changed after planning")
    by_arm, audit = _scan_partition(
        manifest=manifest,
        manifest_path=manifest_path,
        task=task,
    )
    aggregate_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = aggregate_path.with_name(f".{aggregate_path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as handle:
            pickle.dump(
                {
                    "plan_id": plan["plan_id"],
                    "instance_id": instance_id,
                    "by_arm": by_arm,
                    "audit": audit,
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
        "strict_paired_labels": audit.get("strict_paired_labels", 0),
        "dynamic_observed_atom_count": audit.get("dynamic_observed_atom_count", 0),
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


def _merged(values: Sequence[policy_v4.AdditiveAggregate]) -> policy_v4.AdditiveAggregate:
    result = policy_v4.AdditiveAggregate()
    for value in values:
        result.merge(value)
    return result


def _empty_metrics() -> JSONMap:
    return {
        "heldout_decision_count": 0,
        "known_action_count": 0,
        "known_action_coverage": None,
        "top1_accuracy": None,
        "top3_accuracy": None,
        "contextual_log_loss": None,
        "global_log_loss": None,
        "contextual_log_loss_improvement": None,
        "expected_calibration_error": None,
    }


def _action_group(action_label: str) -> str:
    value = json.loads(action_label)
    action_key = value.get("action_key") if isinstance(value, dict) else None
    return "QUEUE_HS_CLEAVE" if action_key in policy_v4.QUEUE_ACTIONS else "OTHER"


def _summary_metrics(
    *,
    total: int,
    known: int,
    top1: int,
    top3: int,
    contextual_loss: float,
    global_loss: float,
) -> JSONMap:
    if not total:
        return _empty_metrics()
    contextual_mean = contextual_loss / total
    global_mean = global_loss / total
    return {
        "heldout_decision_count": total,
        "known_action_count": known,
        "known_action_coverage": known / total,
        "top1_accuracy": top1 / total,
        "top3_accuracy": top3 / total,
        "contextual_log_loss": contextual_mean,
        "global_log_loss": global_mean,
        "contextual_log_loss_improvement": global_mean - contextual_mean,
    }


def evaluate_component_split(
    components: Mapping[str, policy_v4.AdditiveAggregate],
    *,
    fold_count: int,
    split_seed: int,
    alpha: float,
    backoff_strength: float,
) -> JSONMap:
    ids = sorted(components)
    effective, assignment = policy_v2._fold_assignment(
        ids, fold_count=fold_count, split_seed=split_seed
    )
    if effective < 2:
        return {
            "component_count": len(ids),
            "effective_fold_count": effective,
            "component_to_fold": [
                {"component_id": key, "fold_index": assignment[key]}
                for key in sorted(assignment)
            ],
            "each_component_held_out_exactly_once": bool(ids),
            "row_random_split": False,
            "folds": [],
            "metrics": _empty_metrics(),
            "action_group_metrics": {},
        }
    total_n = known_n = top1_n = top3_n = 0
    contextual_loss = global_loss = 0.0
    calibration_rows: list[tuple[float, bool, int]] = []
    folds: list[JSONMap] = []
    match_depth: Counter[str] = Counter()
    family_matches: Counter[str] = Counter()
    groups: dict[str, Counter[str]] = defaultdict(Counter)
    for fold_index in range(effective):
        test_ids = [key for key in ids if assignment[key] == fold_index]
        train_ids = [key for key in ids if assignment[key] != fold_index]
        train = _merged([components[key] for key in train_ids])
        test = _merged([components[key] for key in test_ids])
        catalog = sorted(train.action_counts)
        global_total = sum(train.action_counts.values())
        global_denominator = global_total + alpha * (len(catalog) + 1)
        fold_top1 = fold_n = 0
        for (context_pairs, action), count in sorted(test.decision_cells.items()):
            atoms = tuple(policy_v4.FeatureAtom(*pair) for pair in context_pairs)
            probabilities, matched, unknown = policy_v4.distribution_from_atoms(
                train,
                atoms,
                alpha=alpha,
                strength=backoff_strength,
            )
            ranked = sorted(probabilities, key=lambda key: (-probabilities[key], key))
            known = action in probabilities
            probability = probabilities.get(action, unknown)
            if probability <= 0 or not math.isfinite(probability):
                raise HistoricalFuryV4HpcError("held-out action probability is invalid")
            global_probability = (
                (train.action_counts.get(action, 0) + alpha) / global_denominator
                if global_denominator > 0
                else 1.0
            )
            predicted = ranked[0] if ranked else policy_v2.UNKNOWN_ACTION_KEY
            correct = predicted == action
            in_top3 = action in ranked[:3]
            loss = -math.log(probability)
            global_row_loss = -math.log(global_probability)
            total_n += count
            fold_n += count
            known_n += count if known else 0
            top1_n += count if correct else 0
            fold_top1 += count if correct else 0
            top3_n += count if in_top3 else 0
            contextual_loss += count * loss
            global_loss += count * global_row_loss
            confidence = probabilities.get(predicted, unknown)
            calibration_rows.append((confidence, correct, count))
            match_depth[str(len(matched))] += count
            for family in matched:
                family_matches[family] += count
            group = groups[_action_group(action)]
            group["total"] += count
            group["known"] += count if known else 0
            group["top1"] += count if correct else 0
            group["top3"] += count if in_top3 else 0
            group["contextual_loss_scaled"] += int(round(count * loss * 1e12))
            group["global_loss_scaled"] += int(round(count * global_row_loss * 1e12))
        folds.append(
            {
                "fold_index": fold_index,
                "train_component_ids_sha256": policy_v2._sha256_json(train_ids),
                "test_component_ids_sha256": policy_v2._sha256_json(test_ids),
                "train_component_count": len(train_ids),
                "test_component_count": len(test_ids),
                "train_test_component_overlap_count": 0,
                "heldout_decision_count": fold_n,
                "top1_accuracy": fold_top1 / fold_n if fold_n else None,
            }
        )
    metrics = _summary_metrics(
        total=total_n,
        known=known_n,
        top1=top1_n,
        top3=top3_n,
        contextual_loss=contextual_loss,
        global_loss=global_loss,
    )
    if total_n:
        ece = 0.0
        for bin_index in range(10):
            low = bin_index / 10.0
            high = (bin_index + 1) / 10.0
            rows = [
                row
                for row in calibration_rows
                if row[0] >= low and (row[0] < high or bin_index == 9)
            ]
            weight = sum(row[2] for row in rows)
            if weight:
                average_confidence = sum(row[0] * row[2] for row in rows) / weight
                average_accuracy = sum(bool(row[1]) * row[2] for row in rows) / weight
                ece += (weight / total_n) * abs(average_confidence - average_accuracy)
        metrics["expected_calibration_error"] = ece
    group_metrics = {}
    for name, counts in sorted(groups.items()):
        group_metrics[name] = _summary_metrics(
            total=counts["total"],
            known=counts["known"],
            top1=counts["top1"],
            top3=counts["top3"],
            contextual_loss=counts["contextual_loss_scaled"] / 1e12,
            global_loss=counts["global_loss_scaled"] / 1e12,
        )
    return {
        "component_count": len(ids),
        "effective_fold_count": effective,
        "component_to_fold": [
            {"component_id": key, "fold_index": assignment[key]}
            for key in sorted(assignment)
        ],
        "each_component_held_out_exactly_once": True,
        "row_random_split": False,
        "folds": folds,
        "metrics": metrics,
        "action_group_metrics": group_metrics,
        "context_match_depth_counts": dict(sorted(match_depth.items())),
        "context_family_match_counts": dict(sorted(family_matches.items())),
    }


def reduce(*, plan_path: str | Path) -> JSONMap:
    """Merge sufficient statistics; never reopen a Stage5 partition."""

    started_wall = time.perf_counter()
    started_cpu = time.process_time()
    _, plan = _load_plan(plan_path)
    _live_code_matches(plan)
    _manifest, _manifest_path, source = policy_v2._load_published_input_shallow(
        plan["stage5_manifest"]
    )
    if source != plan["stage5_source"]:
        raise HistoricalFuryV4HpcError("Stage5 manifest changed before reduce")
    admission_path, _bindings, admission_source = hpc_v3._load_admission_bindings(
        plan["admission_manifest"]
    )
    if str(admission_path) != plan["admission_manifest"] or admission_source != plan[
        "admission_source"
    ]:
        raise HistoricalFuryV4HpcError("admission manifest changed before reduce")
    by_arm: dict[str, dict[str, policy_v4.AdditiveAggregate]] = {
        arm: {} for arm in policy_v4.ARMS
    }
    audit: Counter[str] = Counter()
    worker_resources: list[JSONMap] = []
    for task in plan["tasks"]:
        aggregate_path, receipt_path = _task_paths(plan, int(task["position"]))
        if not aggregate_path.is_file() or not receipt_path.is_file():
            raise HistoricalFuryV4HpcError(
                f"worker output incomplete: {task['instance_id']}"
            )
        receipt = _load_json(receipt_path)[1]
        if (
            receipt.get("status") != "COMPLETE"
            or receipt.get("plan_id") != plan["plan_id"]
            or receipt.get("instance_id") != task["instance_id"]
            or receipt.get("node") != task["node"]
        ):
            raise HistoricalFuryV4HpcError("worker receipt differs from plan")
        try:
            with aggregate_path.open("rb") as handle:
                worker = pickle.load(handle)
        except (OSError, EOFError, pickle.UnpicklingError) as error:
            raise HistoricalFuryV4HpcError(
                f"cannot read worker aggregate: {aggregate_path}"
            ) from error
        if (
            worker.get("plan_id") != plan["plan_id"]
            or worker.get("instance_id") != task["instance_id"]
        ):
            raise HistoricalFuryV4HpcError("worker aggregate differs from plan")
        for arm in policy_v4.ARMS:
            _merge_component_maps(by_arm[arm], worker["by_arm"][arm])
        audit.update(worker["audit"])
        worker_resources.append(
            {
                "instance_id": task["instance_id"],
                "node": task["node"],
                **receipt["resource_usage"],
            }
        )
    counts = {
        arm: sum(value.decision_count for value in by_arm[arm].values())
        for arm in policy_v4.ARMS
    }
    component_sets = {arm: set(by_arm[arm]) for arm in policy_v4.ARMS}
    if len(set(counts.values())) != 1 or len({frozenset(x) for x in component_sets.values()}) != 1:
        raise HistoricalFuryV4HpcError("A/B/C labels or component sets differ")
    bc_aggregates_equal = by_arm[policy_v4.ARM_B] == by_arm[policy_v4.ARM_C]
    parameters = plan["fixed_parameters"]
    evaluations = {
        arm: evaluate_component_split(
            by_arm[arm],
            fold_count=int(parameters["fold_count"]),
            split_seed=int(parameters["split_seed"]),
            alpha=float(parameters["smoothing_alpha"]),
            backoff_strength=float(parameters["backoff_strength"]),
        )
        for arm in policy_v4.ARMS
    }
    fold_maps = [evaluations[arm]["component_to_fold"] for arm in policy_v4.ARMS]
    if fold_maps[1:] != fold_maps[:-1]:
        raise HistoricalFuryV4HpcError("A/B/C component folds differ")
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
            "decision_count_by_arm": counts,
            "outer_component_count": len(next(iter(component_sets.values()))),
            "audit": dict(sorted(audit.items())),
            "reference_denominators": {
                "fury_pre_whitelist": EXPECTED_FURY_PRE_WHITELIST_REFERENCE,
                "strict_fury_controllable": EXPECTED_STRICT_FURY_LABELS,
                "all_warrior": EXPECTED_ALL_WARRIOR_REFERENCE,
            },
        },
        "paired_evaluation": evaluations,
        "c_degeneracy": {
            "preregistered_expected": True,
            "aggregate_exactly_equals_b": bc_aggregates_equal,
            "dynamic_observed_atom_count": int(
                audit.get("dynamic_observed_atom_count", 0)
            ),
            "missing_values_imputed": False,
        },
        "resource_usage": {
            "reduce": _resource_usage(started_wall, started_cpu),
            "workers": worker_resources,
        },
        "runner_or_deployment_authorized": False,
    }
    output = Path(str(plan["output_directory"]))
    output.mkdir(parents=True, exist_ok=True)
    _write_json(output / "reduction.json", reduced)
    _write_json(Path(str(plan["work_directory"])) / "reduce.json", reduced)
    return reduced


def _metrics(reduction: Mapping[str, Any], arm: str) -> Mapping[str, Any]:
    evaluations = policy_v2._mapping(
        reduction.get("paired_evaluation"), "paired_evaluation"
    )
    evaluation = policy_v2._mapping(evaluations.get(arm), arm)
    return policy_v2._mapping(evaluation.get("metrics"), f"{arm}.metrics")


def _improvements(candidate: Mapping[str, Any], baseline: Mapping[str, Any]) -> JSONMap:
    return {
        "top1_accuracy": float(candidate["top1_accuracy"])
        > float(baseline["top1_accuracy"]),
        "top3_accuracy": float(candidate["top3_accuracy"])
        > float(baseline["top3_accuracy"]),
        "contextual_log_loss": float(candidate["contextual_log_loss"])
        < float(baseline["contextual_log_loss"]),
        "expected_calibration_error": float(candidate["expected_calibration_error"])
        < float(baseline["expected_calibration_error"]),
    }


def _metric_delta(candidate: Mapping[str, Any], baseline: Mapping[str, Any]) -> JSONMap:
    return {
        key: float(candidate[key]) - float(baseline[key])
        for key in (
            "top1_accuracy",
            "top3_accuracy",
            "contextual_log_loss",
            "expected_calibration_error",
        )
    }


def validate(
    *, plan_path: str | Path, reduction_path: str | Path | None = None
) -> JSONMap:
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
        raise HistoricalFuryV4HpcError("reduction does not belong to this plan")
    accounting = policy_v2._mapping(reduction.get("accounting"), "accounting")
    counts = policy_v2._mapping(
        accounting.get("decision_count_by_arm"), "decision_count_by_arm"
    )
    evaluations = policy_v2._mapping(
        reduction.get("paired_evaluation"), "paired_evaluation"
    )
    degeneracy = policy_v2._mapping(reduction.get("c_degeneracy"), "c_degeneracy")
    a = _metrics(reduction, policy_v4.ARM_A)
    b = _metrics(reduction, policy_v4.ARM_B)
    c = _metrics(reduction, policy_v4.ARM_C)
    expected = plan["expected_crosscheck"]
    component_maps = [evaluations[arm]["component_to_fold"] for arm in policy_v4.ARMS]
    gates = {
        "all_84_workers_present": accounting.get("worker_count") == len(plan["tasks"]),
        "strict_label_count_reproduced": all(
            counts.get(arm) == expected["strict_fury_labels"] for arm in policy_v4.ARMS
        ),
        "paired_arm_counts_equal": len(set(counts.values())) == 1,
        "same_components_and_folds": component_maps[1:] == component_maps[:-1],
        "same_heldout_count": len(
            {a["heldout_decision_count"], b["heldout_decision_count"], c["heldout_decision_count"]}
        )
        == 1,
        "no_hyperparameter_search": plan["fixed_parameters"]["hyperparameter_search"]
        is False,
        "c_aggregate_equals_b_as_preregistered": degeneracy.get(
            "aggregate_exactly_equals_b"
        )
        is True,
        "c_metrics_equal_b_as_preregistered": dict(c) == dict(b),
        "dynamic_atoms_zero_as_preregistered": degeneracy.get(
            "dynamic_observed_atom_count"
        )
        == expected["dynamic_atoms"],
        "missing_values_not_imputed": degeneracy.get("missing_values_imputed")
        is False,
    }
    c_vs_a = _improvements(c, a)
    b_vs_a = _improvements(b, a)
    integrity_pass = all(gates.values())
    joint_improvement = all(c_vs_a.values())
    next_step_allowed = integrity_pass and joint_improvement
    if not integrity_pass:
        validation_status = "INTEGRITY_FAIL_NO_NEXT_STEP"
    elif joint_improvement:
        validation_status = "PASS_FOUR_METRIC_JOINT_IMPROVEMENT_DIAGNOSTIC"
    else:
        validation_status = "RETAIN_NEGATIVE_NO_NEXT_HPC"
    validation: JSONMap = {
        "schema": SCHEMA + "/validation",
        "revision": REVISION,
        "plan_id": plan["plan_id"],
        "status": validation_status,
        "integrity_gates": gates,
        "four_metric_strict_improvement": {
            "primary_c_vs_a": c_vs_a,
            "descriptive_b_vs_a": b_vs_a,
        },
        "metrics": {
            "arm_a": dict(a),
            "arm_b": dict(b),
            "arm_c": dict(c),
            "delta_b_minus_a": _metric_delta(b, a),
            "delta_c_minus_a": _metric_delta(c, a),
        },
        "c_expected_degeneracy_did_not_change_primary_gate": True,
        "next_step_allowed": next_step_allowed,
        "runner_or_deployment_authorized": False,
        "negative_result_retained": not next_step_allowed,
    }
    queue = {
        arm: evaluations[arm]["action_group_metrics"].get("QUEUE_HS_CLEAVE")
        for arm in policy_v4.ARMS
    }
    diagnostic: JSONMap = {
        "schema": SCHEMA + "/diagnostic",
        "revision": REVISION,
        "plan_id": plan["plan_id"],
        "status": "COMPLETE",
        "queue_hs_cleave_metrics": queue,
        "context_match_depth_counts": {
            arm: evaluations[arm]["context_match_depth_counts"]
            for arm in policy_v4.ARMS
        },
        "context_family_match_counts": {
            arm: evaluations[arm]["context_family_match_counts"]
            for arm in policy_v4.ARMS
        },
        "c_degeneracy": dict(degeneracy),
        "scientific_decision": validation_status,
    }
    output = Path(str(plan["output_directory"]))
    _write_json(output / "validation.json", validation)
    _write_json(output / "diagnostic.json", diagnostic)
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
        work = args.work_directory or output / ".hpc-v4-abc84-20260911-r1"
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
