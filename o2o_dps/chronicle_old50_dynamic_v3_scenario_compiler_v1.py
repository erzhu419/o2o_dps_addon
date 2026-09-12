"""Compile exact-wave Chronicle background blocks into dynamic-v3 scenarios.

The compiler consumes the compact old-50 target capsule together with one
content-addressed Stage-6 block and the exact Fury focal-GUID join produced by
``chronicle_stage6_old50_overlap_hpc_v1``.  It never draws another wave from a
component: the teammate schedule is the same wave, projected to the capsule
pile, with the focal player's events removed by exact GUID.

Health, base armor, debuff magnitudes, attackability, classification, and
position remain explicit simulator hypotheses.  The resulting scenario is
executable by ``load_dynamic_v3`` but permanently diagnostic/non-voting.
"""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
import json
import math
import re
from typing import Any, Mapping, Sequence

from . import chronicle_external_team_background_generator_v2 as background_v2
from . import chronicle_stage6_old50_overlap_hpc_v1 as overlap_v1
from .fury_capsule_execution_binding_v2 import (
    _static_request_target,
    _validate_base_request,
    enumerate_capsule_scenarios_v2,
)
from .fury_contra_adapter_v2 import ContraTargetClassificationV2
from .fury_encounter_scenarios_v1 import ARMOR_STAT_INDEX, HEALTH_STAT_INDEX
from .fury_offline_scenario_capsule_v2 import ARMOR_DEBUFF_REGISTRY
from .fury_paired_multiseed_runner_v4 import (
    normalize_runner_scenarios,
    sha256_json,
)
from .sim_bridge import BackgroundDamageEventV1, DynamicTargetHealthV1
from .sim_bridge_dynamic_v2 import (
    DynamicAttackabilityEventV2,
    DynamicEffectiveArmorEventV2,
)
from .sim_bridge_dynamic_v3 import DynamicTargetSemanticsConfigV3


JSONMap = dict[str, Any]

SCHEMA = "chronicle_old50_dynamic_v3_scenario_compiler/v1"
REVISION = "exact_wave_fury_leave_one_out_hypothesis_grid_v1"
STATUS = "DYNAMIC_V3_EXECUTABLE_DIAGNOSTIC_NONVOTING"
TARGET_CONTEXT_SCHEMA = "fury_dynamic_v5_target_context_bundle/v4"
SCENARIO_MODEL_SCHEMA = "chronicle_old50_dynamic_v3_scenario_model/v1"
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_HYPOTHESIS_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")

LIMITATION_CODES = (
    "TARGET_HEALTH_IS_CAPSULE_SENSITIVITY_HYPOTHESIS",
    "INITIAL_BASE_ARMOR_IS_CALLER_SELECTED_HYPOTHESIS",
    "BASE_AND_EFFECTIVE_ARMOR_ARE_SIMULATOR_HYPOTHESES",
    "ATTACKABILITY_IS_CAPSULE_WINDOW_HYPOTHESIS",
    "TARGET_CLASSIFICATION_IS_CALLER_SELECTED_HYPOTHESIS",
    "TEAM_SCHEDULE_IS_FIXED_OBSERVED_LEAVE_ONE_OUT_NOT_ENDOGENOUS",
    "SUBSET_PILES_EXCLUDE_OTHER_SAME_WAVE_TARGETS_WHEN_APPLICABLE",
    "OFFLINE_SCENARIO_NOT_REAL_CLIENT_FIDELITY",
)


class ChronicleOld50DynamicV3CompilerError(RuntimeError):
    """The exact-wave inputs cannot be compiled without widening their claims."""


@dataclass(frozen=True)
class CompiledOld50DynamicV3ScenarioV1:
    artifact: JSONMap
    scenario: JSONMap
    dynamic_config: DynamicTargetSemanticsConfigV3


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ChronicleOld50DynamicV3CompilerError(f"{label} must be an object")
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ChronicleOld50DynamicV3CompilerError(f"{label} must be an array")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChronicleOld50DynamicV3CompilerError(f"{label} must be nonempty text")
    return value


def _integer(value: Any, label: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ChronicleOld50DynamicV3CompilerError(f"{label} must be an integer")
    if value < (1 if positive else 0):
        qualifier = "positive" if positive else "nonnegative"
        raise ChronicleOld50DynamicV3CompilerError(f"{label} must be {qualifier}")
    return value


def _number(value: Any, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ChronicleOld50DynamicV3CompilerError(f"{label} must be numeric")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < (0.0 if not positive else 0.0):
        raise ChronicleOld50DynamicV3CompilerError(f"{label} must be finite")
    if positive and parsed <= 0:
        raise ChronicleOld50DynamicV3CompilerError(f"{label} must be positive")
    return parsed


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ChronicleOld50DynamicV3CompilerError(
            f"{label} must be a lowercase SHA-256"
        )
    return value


def _strict_json(value: Any, label: str) -> Any:
    try:
        return json.loads(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ChronicleOld50DynamicV3CompilerError(
            f"{label} is not strict JSON: {error}"
        ) from error


def _find_capsule_scenario(
    bundle: Mapping[str, Any], scenario_id: str
) -> tuple[Mapping[str, Any], str]:
    scenarios = enumerate_capsule_scenarios_v2(bundle)
    matches = [row for row in scenarios if row.get("scenario_id") == scenario_id]
    if len(matches) != 1:
        raise ChronicleOld50DynamicV3CompilerError(
            "compiler input scenario_id is absent or duplicated in the old-50 capsule"
        )
    source_id = _text(matches[0].get("source_bundle_id"), "source_bundle_id")
    catalogs = {
        _text(row.get("source_bundle_id"), "capsule source_bundle_id"): _sha256(
            _mapping(row.get("catalog"), "capsule source catalog").get("sha256"),
            "capsule catalog SHA-256",
        )
        for row in _array(bundle.get("sources"), "capsule sources")
        if isinstance(row, Mapping)
    }
    if source_id not in catalogs:
        raise ChronicleOld50DynamicV3CompilerError(
            "capsule scenario source catalog is missing"
        )
    return matches[0], catalogs[source_id]


def _wave_key(value: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        _text(value.get("instance_id"), "instance_id"),
        _text(value.get("encounter_id"), "encounter_id"),
        _text(value.get("wave_id"), "wave_id"),
    )


def _validate_exact_inputs(
    *,
    compiler_input: Mapping[str, Any],
    capsule_bundle: Mapping[str, Any],
    stage6_block: Mapping[str, Any],
) -> tuple[Mapping[str, Any], str, tuple[str, ...], Mapping[str, int]]:
    try:
        overlap_v1.validate_compiler_input(compiler_input)
    except Exception as error:
        raise ChronicleOld50DynamicV3CompilerError(
            f"invalid overlap compiler input: {error}"
        ) from error
    identity = _mapping(compiler_input.get("identity"), "compiler identity")
    scenario_id = _text(identity.get("scenario_id"), "scenario_id")
    scenario, catalog_sha = _find_capsule_scenario(capsule_bundle, scenario_id)
    source = _mapping(scenario.get("source_identity"), "capsule source_identity")
    block_wave = _mapping(stage6_block.get("wave"), "Stage-6 wave")
    if _wave_key(identity) != _wave_key(source) or _wave_key(source) != _wave_key(
        block_wave
    ):
        raise ChronicleOld50DynamicV3CompilerError(
            "compiler input, capsule, and Stage-6 block do not share one exact wave key"
        )
    if source.get("wave_ordinal") != block_wave.get("wave_ordinal"):
        raise ChronicleOld50DynamicV3CompilerError(
            "capsule and Stage-6 wave ordinals differ after exact-key join"
        )
    expected_block_sha = _sha256(
        _mapping(
            _mapping(compiler_input.get("source_bindings"), "source_bindings").get(
                "stage6_exact_block"
            ),
            "stage6_exact_block",
        ).get("block_content_sha256"),
        "Stage-6 block SHA-256",
    )
    try:
        observed_block_sha = background_v2._verify_content_address(
            stage6_block, label="Stage-6 exact block"
        )
        background_v2._validate_block(
            stage6_block, expected_instance_id=_wave_key(identity)[0]
        )
    except Exception as error:
        raise ChronicleOld50DynamicV3CompilerError(
            f"invalid Stage-6 exact block: {error}"
        ) from error
    if observed_block_sha != expected_block_sha:
        raise ChronicleOld50DynamicV3CompilerError(
            "Stage-6 block content address differs from compiler input"
        )
    capsule_binding = _mapping(
        _mapping(compiler_input.get("source_bindings"), "source_bindings").get(
            "old50_capsule"
        ),
        "old50_capsule binding",
    )
    if (
        capsule_binding.get("scenario_id") != scenario_id
        or capsule_binding.get("scenario_capsule_sha256")
        != scenario.get("capsule_sha256")
    ):
        raise ChronicleOld50DynamicV3CompilerError(
            "capsule scenario content address differs from compiler input"
        )
    bundle_sha = _sha256(
        _mapping(capsule_bundle.get("content_address"), "capsule content_address").get(
            "sha256"
        ),
        "capsule bundle SHA-256",
    )
    if capsule_binding.get("bundle_content_sha256") != bundle_sha:
        raise ChronicleOld50DynamicV3CompilerError(
            "capsule bundle content address differs from compiler input"
        )

    scenario_targets = [
        _mapping(row, "capsule target")
        for row in _array(scenario.get("targets"), "capsule targets")
    ]
    selected = tuple(
        _text(row.get("target_guid"), "capsule target GUID")
        for row in scenario_targets
    )
    join = _mapping(compiler_input.get("join"), "compiler join")
    if list(selected) != join.get("selected_target_guids"):
        raise ChronicleOld50DynamicV3CompilerError(
            "compiler selected GUID order differs from capsule targets"
        )
    registry = [
        _mapping(row, "Stage-6 target registry row")
        for row in _array(stage6_block.get("target_registry"), "target_registry")
    ]
    stage6_by_guid = {
        _text(row.get("target_guid"), "Stage-6 target GUID"): _integer(
            row.get("target_index"), "Stage-6 target index"
        )
        for row in registry
    }
    if not set(selected) <= set(stage6_by_guid):
        raise ChronicleOld50DynamicV3CompilerError(
            "capsule target is absent from the exact Stage-6 block"
        )
    capsule_by_guid = {
        guid: _integer(scenario_targets[index].get("target_index"), "capsule index")
        for index, guid in enumerate(selected)
    }
    if tuple(capsule_by_guid.values()) != tuple(range(len(selected))):
        raise ChronicleOld50DynamicV3CompilerError(
            "capsule target indexes are not contiguous"
        )
    expected_remap = [
        {
            "target_guid": guid,
            "stage6_target_index": stage6_by_guid[guid],
            "capsule_target_index": capsule_by_guid[guid],
        }
        for guid in selected
    ]
    if join.get("target_index_remap") != expected_remap:
        raise ChronicleOld50DynamicV3CompilerError(
            "compiler target-index remap differs from exact block and capsule"
        )
    relation = "EQUAL" if set(selected) == set(stage6_by_guid) else "STRICT_SUBSET"
    if join.get("target_guid_relation") != relation:
        raise ChronicleOld50DynamicV3CompilerError(
            "compiler target GUID relation differs from the exact block"
        )
    return scenario, catalog_sha, selected, capsule_by_guid


def _select_health(
    target: Mapping[str, Any], branch_id: str
) -> tuple[int, Mapping[str, Any]]:
    family = _mapping(
        target.get("max_health_hypothesis_family"), "max_health_hypothesis_family"
    )
    branches = [
        _mapping(row, "health branch")
        for row in _array(family.get("shared_branch_family"), "health branches")
        if isinstance(row, Mapping) and row.get("branch_id") == branch_id
    ]
    if len(branches) != 1:
        raise ChronicleOld50DynamicV3CompilerError(
            f"target lacks exactly one health branch {branch_id!r}"
        )
    branch = branches[0]
    if branch.get("use_health") is not True:
        raise ChronicleOld50DynamicV3CompilerError(
            "dynamic-v3 compilation requires a health-enabled branch"
        )
    value = _number(branch.get("max_health"), "health branch max_health", positive=True)
    if not value.is_integer():
        raise ChronicleOld50DynamicV3CompilerError(
            "health branch max_health must be integer-valued"
        )
    return int(value), branch


def _magnitude_registry() -> dict[str, tuple[Mapping[str, Any], ...]]:
    values: dict[str, dict[str, Mapping[str, Any]]] = defaultdict(dict)
    for raw in ARMOR_DEBUFF_REGISTRY.values():
        debuff = _mapping(raw, "armor debuff registry row")
        debuff_id = _text(debuff.get("debuff_id"), "debuff_id")
        for raw_hypothesis in _array(
            debuff.get("magnitude_hypotheses"), "magnitude hypotheses"
        ):
            hypothesis = _mapping(raw_hypothesis, "magnitude hypothesis")
            values[debuff_id][sha256_json(hypothesis)] = hypothesis
    return {
        debuff_id: tuple(rows[key] for key in sorted(rows))
        for debuff_id, rows in values.items()
    }


def _select_magnitudes(
    observed_debuff_ids: Sequence[str],
    choices: Mapping[str, int | float],
) -> dict[str, Mapping[str, Any]]:
    if not isinstance(choices, Mapping):
        raise ChronicleOld50DynamicV3CompilerError(
            "armor_magnitude_by_debuff must be a mapping"
        )
    registry = _magnitude_registry()
    selected: dict[str, Mapping[str, Any]] = {}
    for debuff_id in sorted(set(observed_debuff_ids)):
        candidates = registry.get(debuff_id)
        if not candidates:
            raise ChronicleOld50DynamicV3CompilerError(
                f"unregistered observed armor debuff {debuff_id!r}"
            )
        if debuff_id in choices:
            desired = _number(
                choices[debuff_id], f"armor magnitude choice {debuff_id}", positive=True
            )
            matches = [
                row
                for row in candidates
                if _number(row.get("value"), "registered magnitude", positive=True)
                == desired
            ]
            if len(matches) != 1:
                raise ChronicleOld50DynamicV3CompilerError(
                    f"armor magnitude {desired:g} is not a unique registered branch "
                    f"for {debuff_id}"
                )
            selected[debuff_id] = matches[0]
        elif len(candidates) == 1:
            selected[debuff_id] = candidates[0]
        else:
            values = sorted({row.get("value") for row in candidates})
            raise ChronicleOld50DynamicV3CompilerError(
                f"armor debuff {debuff_id} is ambiguous; choose one of {values}"
            )
    extras = sorted(set(choices) - set(observed_debuff_ids))
    if extras:
        raise ChronicleOld50DynamicV3CompilerError(
            f"armor magnitude choices contain unobserved debuffs: {extras}"
        )
    return selected


def _armor_reduction(stacks: int, hypothesis: Mapping[str, Any]) -> float:
    value = _number(hypothesis.get("value"), "armor magnitude", positive=True)
    kind = hypothesis.get("kind")
    maximum = _integer(hypothesis.get("max_stacks"), "max_stacks", positive=True)
    if stacks > maximum:
        raise ChronicleOld50DynamicV3CompilerError(
            f"observed armor stacks {stacks} exceed selected maximum {maximum}"
        )
    if kind == "flat_armor_per_stack":
        return value * stacks
    if kind in {"flat_armor", "flat_armor_at_five_combo_points"}:
        return value if stacks > 0 else 0.0
    raise ChronicleOld50DynamicV3CompilerError(
        f"unsupported armor magnitude kind {kind!r}"
    )


def _compile_armor_events(
    targets: Sequence[Mapping[str, Any]],
    *,
    base_armor: float,
    magnitude_choices: Mapping[str, int | float],
    horizon_ms: int,
) -> tuple[tuple[DynamicEffectiveArmorEventV2, ...], JSONMap]:
    transitions_by_target: list[list[Mapping[str, Any]]] = []
    observed_ids: list[str] = []
    for target in targets:
        armor = _mapping(target.get("armor"), "target armor")
        transitions = [
            _mapping(row, "armor transition")
            for row in _array(armor.get("observed_transitions"), "armor transitions")
        ]
        transitions_by_target.append(transitions)
        observed_ids.extend(_text(row.get("debuff_id"), "debuff_id") for row in transitions)
    selected = _select_magnitudes(observed_ids, magnitude_choices)
    raw_events: list[tuple[int, int, float]] = []
    for target_index, transitions in enumerate(transitions_by_target):
        active: dict[str, int] = {}
        grouped: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
        for transition in transitions:
            time_ms = _integer(transition.get("offset_ms"), "armor offset_ms")
            if time_ms > horizon_ms:
                raise ChronicleOld50DynamicV3CompilerError(
                    "armor transition exceeds the scenario horizon"
                )
            grouped[time_ms].append(transition)
        raw_events.append((0, target_index, base_armor))
        for time_ms in sorted(grouped):
            rows = sorted(
                grouped[time_ms],
                key=lambda row: (
                    _integer(
                        _mapping(row.get("anchor"), "armor anchor").get("event_index"),
                        "armor anchor event_index",
                    ),
                    str(row.get("debuff_id")),
                ),
            )
            for row in rows:
                debuff_id = _text(row.get("debuff_id"), "debuff_id")
                stacks = _integer(row.get("stacks"), "armor stacks")
                operation = row.get("operation")
                if operation == "set" and stacks > 0:
                    active[debuff_id] = stacks
                elif operation == "remove" and stacks == 0:
                    active.pop(debuff_id, None)
                else:
                    raise ChronicleOld50DynamicV3CompilerError(
                        "armor transition operation/stacks pair is inconsistent"
                    )
            reduction = sum(
                _armor_reduction(stacks, selected[debuff_id])
                for debuff_id, stacks in active.items()
            )
            effective = max(0.0, base_armor - reduction)
            if time_ms == 0:
                raw_events[-1] = (0, target_index, effective)
            else:
                raw_events.append((time_ms, target_index, effective))
    raw_events.sort(key=lambda row: (row[0], row[1]))
    events = tuple(
        DynamicEffectiveArmorEventV2(index, time_ms, target_index, armor)
        for index, (time_ms, target_index, armor) in enumerate(raw_events)
    )
    receipt = {
        "base_armor": base_armor,
        "selected_magnitudes": {
            key: _strict_json(value, f"selected armor magnitude {key}")
            for key, value in sorted(selected.items())
        },
        "event_count": len(events),
        "effective_armor_is_historical_truth": False,
    }
    return events, receipt


def _selected_windows(
    target: Mapping[str, Any], branch_id: str, horizon_ms: int
) -> list[tuple[int, int]]:
    matches = [
        _mapping(row, "attackability branch")
        for row in _array(
            target.get("attackable_window_hypothesis_family"),
            "attackability branches",
        )
        if isinstance(row, Mapping) and row.get("branch_id") == branch_id
    ]
    if len(matches) != 1:
        raise ChronicleOld50DynamicV3CompilerError(
            f"target lacks exactly one attackability branch {branch_id!r}"
        )
    windows: list[tuple[int, int]] = []
    for raw in _array(matches[0].get("windows"), "attackability windows"):
        pair = _array(raw, "attackability window")
        if len(pair) != 2:
            raise ChronicleOld50DynamicV3CompilerError(
                "attackability window must contain start and end"
            )
        start = _integer(pair[0], "attackability start")
        end = _integer(pair[1], "attackability end")
        if end < start or end > horizon_ms:
            raise ChronicleOld50DynamicV3CompilerError(
                "attackability window lies outside the scenario horizon"
            )
        if windows and start <= windows[-1][1] + 1:
            windows[-1] = (windows[-1][0], max(windows[-1][1], end))
        else:
            windows.append((start, end))
    if not windows:
        raise ChronicleOld50DynamicV3CompilerError(
            "attackability branch has no windows"
        )
    return windows


def _compile_attackability_events(
    targets: Sequence[Mapping[str, Any]], *, branch_id: str, horizon_ms: int
) -> tuple[tuple[DynamicAttackabilityEventV2, ...], JSONMap]:
    raw_events: list[tuple[int, int, bool]] = []
    windows_receipt: list[JSONMap] = []
    for target_index, target in enumerate(targets):
        windows = _selected_windows(target, branch_id, horizon_ms)
        windows_receipt.append(
            {"target_index": target_index, "windows_inclusive_ms": [list(row) for row in windows]}
        )
        if windows[0][0] > 0:
            raw_events.append((0, target_index, False))
        for start, end in windows:
            if start > 0:
                raw_events.append((start, target_index, True))
            if end < horizon_ms:
                raw_events.append((end + 1, target_index, False))
    raw_events.sort(key=lambda row: (row[0], row[1], row[2]))
    events = tuple(
        DynamicAttackabilityEventV2(index, time_ms, target_index, attackable)
        for index, (time_ms, target_index, attackable) in enumerate(raw_events)
    )
    return events, {
        "branch_id": branch_id,
        "windows": windows_receipt,
        "inclusive_end_compiled_as_false_at_end_plus_one_ms": True,
        "event_count": len(events),
        "attackability_is_historical_truth": False,
    }


def _compile_background_events(
    *,
    block: Mapping[str, Any],
    selected_guids: Sequence[str],
    capsule_index_by_guid: Mapping[str, int],
    focal_guid: str,
    horizon_ms: int,
    focal_upper_bounds: Mapping[str, Any],
) -> tuple[tuple[BackgroundDamageEventV1, ...], JSONMap]:
    selected_set = set(selected_guids)
    kept: list[Mapping[str, Any]] = []
    selected_focal: list[Mapping[str, Any]] = []
    excluded_other_target = 0
    for raw in _array(block.get("runtime_candidate_schedule"), "runtime schedule"):
        event = _mapping(raw, "runtime event")
        guid = _text(event.get("target_guid"), "runtime target_guid")
        actor = _text(event.get("actor_player_guid"), "runtime actor_player_guid")
        time_ms = _integer(event.get("time_ms"), "runtime time_ms")
        if time_ms > horizon_ms:
            if guid in selected_set:
                raise ChronicleOld50DynamicV3CompilerError(
                    "selected Stage-6 event exceeds capsule horizon"
                )
            excluded_other_target += 1
            continue
        if guid not in selected_set:
            excluded_other_target += 1
            continue
        if actor == focal_guid:
            selected_focal.append(event)
            continue
        kept.append(event)
    typed = tuple(
        BackgroundDamageEventV1(
            schedule_index=index,
            time_ms=_integer(event.get("time_ms"), "background time_ms"),
            target_index=capsule_index_by_guid[
                _text(event.get("target_guid"), "background target_guid")
            ],
            event_id=_text(event.get("event_id"), "background event_id"),
            damage=_number(event.get("damage"), "background damage", positive=True),
        )
        for index, event in enumerate(kept)
    )
    expected_count = _integer(
        focal_upper_bounds.get("excluded_focal_event_count"),
        "focal excluded event upper bound",
    )
    expected_damage = _number(
        focal_upper_bounds.get("excluded_focal_damage_amount"),
        "focal excluded damage upper bound",
    )
    selected_focal_damage = sum(
        _number(row.get("damage"), "selected focal damage", positive=True)
        for row in selected_focal
    )
    if len(selected_focal) > expected_count or selected_focal_damage > expected_damage:
        raise ChronicleOld50DynamicV3CompilerError(
            "selected focal runtime damage exceeds the Stage-5 exact-GUID leave-one-out receipt"
        )
    return typed, {
        "selection_unit": "SAME_EXACT_STAGE6_BLOCK",
        "focal_player_guid": focal_guid,
        "exact_guid_leave_one_out": True,
        "kept_background_event_count": len(typed),
        "kept_background_damage": sum(row.damage for row in typed),
        "selected_focal_runtime_event_count_removed": len(selected_focal),
        "selected_focal_runtime_damage_removed": selected_focal_damage,
        "stage5_focal_all_event_count_upper_bound": expected_count,
        "stage5_focal_all_attributed_damage_upper_bound": expected_damage,
        "unselected_target_runtime_event_count_removed": excluded_other_target,
        "component_wide_random_draw_used": False,
        "future_schedule_exported_to_policy_context": False,
    }


def _context_evidence(
    *, kind: str, source_sha256: str | None = None, hypothesis_id: str | None = None
) -> JSONMap:
    return {
        "schema": "contra_field_evidence/v2",
        "kind": kind,
        "source_sha256": source_sha256,
        "corpus_sha256": None,
        "hypothesis_id": hypothesis_id,
    }


def _target_context_bundle(
    *,
    targets: Sequence[Mapping[str, Any]],
    health: Sequence[int],
    target_classification: str,
    equipped_item_names: Sequence[str],
    request_sha256: str,
    capsule_scenario_sha256: str,
    health_hypothesis_id: str,
    position_hypothesis_id: str,
) -> JSONMap:
    try:
        classification = ContraTargetClassificationV2(target_classification).value
    except ValueError as error:
        raise ChronicleOld50DynamicV3CompilerError(
            f"unsupported target classification hypothesis {target_classification!r}"
        ) from error
    names = tuple(
        _text(value, f"equipped_item_names[{index}]")
        for index, value in enumerate(equipped_item_names)
    )
    equipment_sha = sha256_json({"equipped_item_names": list(names)})
    contexts: list[JSONMap] = []
    for index, target in enumerate(targets):
        target_name = target.get("display_name") or target.get("target_guid")
        target_name = _text(target_name, "target name")
        contexts.append(
            {
                "context_id": f"old50-dynamic-v3-target-{index}",
                "mode": "SIMULATOR_HYPOTHESIS",
                "target_index": index,
                "target_classification": classification,
                "target_name": target_name,
                "equipped_item_names": list(names),
                "target_max_health": health[index],
                "health_pct_schedule": [],
                "field_evidence": {
                    "target_health_pct": _context_evidence(
                        kind="SENSITIVITY_HYPOTHESIS",
                        hypothesis_id=health_hypothesis_id,
                    ),
                    "target_max_health": _context_evidence(
                        kind="SENSITIVITY_HYPOTHESIS",
                        hypothesis_id=health_hypothesis_id,
                    ),
                    "target_classification": _context_evidence(
                        kind="SENSITIVITY_HYPOTHESIS",
                        hypothesis_id=f"classification-{classification}-v1",
                    ),
                    "target_name": _context_evidence(
                        kind="OBSERVED_SOURCE", source_sha256=capsule_scenario_sha256
                    ),
                    "equipped_item_names": _context_evidence(
                        kind="PINNED_STATIC_INPUT", source_sha256=equipment_sha
                    ),
                    "target_position": _context_evidence(
                        kind="SENSITIVITY_HYPOTHESIS",
                        hypothesis_id=position_hypothesis_id,
                    ),
                },
            }
        )
    return {
        "schema": TARGET_CONTEXT_SCHEMA,
        "status": "OLD50_EXPLICIT_HYPOTHESES_CONTEXT_BOUND",
        "request_sha256": request_sha256,
        "target_count": len(targets),
        "contexts": contexts,
        "bridge_execution_eligible": True,
        "comparison_eligible": False,
        "limitation_codes": list(LIMITATION_CODES),
    }


def _compile_request(
    *,
    base_request: Mapping[str, Any],
    targets: Sequence[Mapping[str, Any]],
    target_health: Sequence[int],
    target_level: int,
    base_armor: float,
    horizon_ms: int,
) -> JSONMap:
    base = _validate_base_request(base_request)
    request = deepcopy(base)
    encounter = dict(_mapping(request.get("encounter"), "base request encounter"))
    request["encounter"] = encounter
    template = _mapping(
        _array(encounter.get("targets"), "base request targets")[0],
        "base request target template",
    )
    request_targets: list[JSONMap] = []
    for index, target in enumerate(targets):
        compiled = _static_request_target(
            template,
            target,
            target_level=target_level,
            base_armor=base_armor,
        )
        stats = list(compiled["stats"])
        stats[ARMOR_STAT_INDEX] = base_armor
        stats[HEALTH_STAT_INDEX] = target_health[index]
        compiled["stats"] = stats
        request_targets.append(compiled)
    encounter["duration"] = horizon_ms / 1000.0
    encounter["durationVariation"] = 0
    encounter["useHealth"] = True
    encounter["targets"] = request_targets
    return _strict_json(request, "compiled simulator request")


def compile_old50_dynamic_v3_scenario_v1(
    *,
    compiler_input: Mapping[str, Any],
    capsule_bundle: Mapping[str, Any],
    stage6_block: Mapping[str, Any],
    base_request: Mapping[str, Any],
    equipped_item_names: Sequence[str],
    target_level: int,
    initial_base_armor: int | float,
    health_branch_id: str,
    attackability_branch_id: str,
    target_classification: str,
    armor_magnitude_by_debuff: Mapping[str, int | float] | None = None,
) -> CompiledOld50DynamicV3ScenarioV1:
    """Compile one focal-player scenario without exposing future team events."""

    level = _integer(target_level, "target_level", positive=True)
    selected_base_armor = _number(
        initial_base_armor, "initial_base_armor", positive=True
    )
    health_branch = _text(health_branch_id, "health_branch_id")
    attack_branch = _text(attackability_branch_id, "attackability_branch_id")
    if _HYPOTHESIS_RE.fullmatch(health_branch) is None or _HYPOTHESIS_RE.fullmatch(
        attack_branch
    ) is None:
        raise ChronicleOld50DynamicV3CompilerError(
            "hypothesis branch IDs are not normalized"
        )
    scenario_source, catalog_sha, selected_guids, capsule_index = (
        _validate_exact_inputs(
            compiler_input=compiler_input,
            capsule_bundle=capsule_bundle,
            stage6_block=stage6_block,
        )
    )
    targets = [
        _mapping(row, "capsule target")
        for row in _array(scenario_source.get("targets"), "capsule targets")
    ]
    horizon_ms = _integer(
        _mapping(scenario_source.get("horizon"), "capsule horizon").get(
            "milliseconds"
        ),
        "horizon_ms",
        positive=True,
    )
    health_rows = [_select_health(target, health_branch) for target in targets]
    target_health = [row[0] for row in health_rows]
    capsule_legacy_armor_family = [
        _mapping(row, "base armor branch")
        for row in _array(
            scenario_source.get("base_armor_hypothesis_family"),
            "base armor family",
        )
    ]
    if not capsule_legacy_armor_family:
        raise ChronicleOld50DynamicV3CompilerError(
            "scenario must retain at least one legacy static base-armor hypothesis"
        )
    capsule_legacy_base_armor_values = sorted(
        {
            _number(row.get("base_armor"), "legacy capsule base armor")
            for row in capsule_legacy_armor_family
        }
    )
    request = _compile_request(
        base_request=base_request,
        targets=targets,
        target_health=target_health,
        target_level=level,
        base_armor=selected_base_armor,
        horizon_ms=horizon_ms,
    )
    focal = _mapping(
        _mapping(
            compiler_input.get("causal_schedule_projection"), "causal projection"
        ).get("focal_leave_one_out"),
        "focal leave-one-out",
    )
    focal_guid = _text(focal.get("focal_player_guid"), "focal_player_guid")
    identity_focal_guid = _text(
        _mapping(compiler_input.get("identity"), "compiler identity").get(
            "focal_player_guid"
        ),
        "identity focal_player_guid",
    )
    if focal_guid != identity_focal_guid:
        raise ChronicleOld50DynamicV3CompilerError(
            "focal leave-one-out GUID differs from compiler identity"
        )
    roster = set(
        _text(value, "roster player GUID")
        for value in _array(stage6_block.get("roster_player_guids"), "Stage-6 roster")
    )
    if focal_guid not in roster:
        raise ChronicleOld50DynamicV3CompilerError(
            "focal Fury GUID is absent from the exact Stage-6 roster"
        )
    background_events, background_receipt = _compile_background_events(
        block=stage6_block,
        selected_guids=selected_guids,
        capsule_index_by_guid=capsule_index,
        focal_guid=focal_guid,
        horizon_ms=horizon_ms,
        focal_upper_bounds=focal,
    )
    attackability_events, attack_receipt = _compile_attackability_events(
        targets, branch_id=attack_branch, horizon_ms=horizon_ms
    )
    armor_events, armor_receipt = _compile_armor_events(
        targets,
        base_armor=selected_base_armor,
        magnitude_choices=armor_magnitude_by_debuff or {},
        horizon_ms=horizon_ms,
    )
    dynamic_config = DynamicTargetSemanticsConfigV3(
        target_health=tuple(
            DynamicTargetHealthV1(index, float(value))
            for index, value in enumerate(target_health)
        ),
        idle_advance_horizon_ms=horizon_ms,
        background_damage_events=background_events,
        attackability_events=attackability_events,
        effective_armor_events=armor_events,
    )
    request_sha = sha256_json(request)
    projection = _mapping(
        scenario_source.get("runner_projection"), "runner_projection"
    )
    provenance = _mapping(
        scenario_source.get("provenance_hashes"), "provenance_hashes"
    )
    component_relation = _mapping(
        _mapping(compiler_input.get("join"), "compiler join").get(
            "component_relation"
        ),
        "component relation",
    )
    source_scenario_id = _text(scenario_source.get("scenario_id"), "scenario_id")
    compiled_id = (
        f"{source_scenario_id}__focal-{focal_guid}__health-{health_branch}"
        f"__armor-{selected_base_armor:g}__attack-{attack_branch}"
        f"__class-{target_classification}"
    )
    health_hypothesis_id = f"old50-{health_branch}-v1"
    layout = _mapping(scenario_source.get("layout"), "scenario layout")
    position_value = _mapping(
        layout.get("spatial_assumption"), "spatial assumption"
    ).get("value")
    position_token = re.sub(r"[^A-Za-z0-9._:/-]+", "-", str(position_value)).strip("-")
    position_hypothesis_id = f"old50-position-{position_token or 'unknown'}-v1"
    context_bundle = _target_context_bundle(
        targets=targets,
        health=target_health,
        target_classification=target_classification,
        equipped_item_names=equipped_item_names,
        request_sha256=request_sha,
        capsule_scenario_sha256=_sha256(
            scenario_source.get("capsule_sha256"), "scenario capsule SHA-256"
        ),
        health_hypothesis_id=health_hypothesis_id,
        position_hypothesis_id=position_hypothesis_id,
    )
    compiler_input_sha = _sha256(
        _mapping(compiler_input.get("content_address"), "compiler content_address").get(
            "sha256"
        ),
        "compiler input SHA-256",
    )
    selection_receipt = {
        "target_level": level,
        "target_level_status": "CALLER_SELECTED_SENSITIVITY_HYPOTHESIS",
        "initial_base_armor": selected_base_armor,
        "initial_base_armor_status": "CALLER_SELECTED_SENSITIVITY_HYPOTHESIS",
        "capsule_legacy_static_base_armor_values": (
            capsule_legacy_base_armor_values
        ),
        "capsule_legacy_static_armor_reused_implicitly": False,
        "health_branch_id": health_branch,
        "health_by_target": [
            {
                "target_index": index,
                "target_guid": selected_guids[index],
                "max_health": value,
                "source": health_rows[index][1].get("source"),
            }
            for index, value in enumerate(target_health)
        ],
        "attackability": attack_receipt,
        "armor": armor_receipt,
        "target_classification": target_classification,
        "classification_is_historical_truth": False,
        "position_is_historical_truth": False,
    }
    scenario_model = {
        "schema": SCENARIO_MODEL_SCHEMA,
        "status": STATUS,
        "request_sha256": request_sha,
        "bridge_execution_eligible": True,
        "historical_truth": False,
        "comparison_eligible": False,
        "source": {
            "compiler_input_sha256": compiler_input_sha,
            "capsule_scenario_sha256": scenario_source["capsule_sha256"],
            "stage6_block_sha256": _mapping(
                stage6_block.get("content_address"), "block content_address"
            )["sha256"],
            "focal_player_guid": focal_guid,
        },
        "hypothesis_selection": selection_receipt,
        "causal_background_projection": background_receipt,
        "limitation_codes": list(LIMITATION_CODES),
    }
    candidate = {
        "instance_id": _wave_key(_mapping(compiler_input["identity"], "identity"))[0],
        "component_id": _text(
            component_relation.get("stage6_component_id"), "Stage-6 component_id"
        ),
        "scenario_id": compiled_id,
        "stratum": _text(projection.get("stratum"), "scenario stratum"),
        "scenario_weight": _number(
            projection.get("scenario_weight"), "scenario weight", positive=True
        ),
        "horizon_ms": horizon_ms,
        "estimated_cost_units": max(
            1, horizon_ms * len(targets) + len(background_events)
        ),
        "request": request,
        "dynamic_load_config": dynamic_config.to_wire(),
        "scenario_model": scenario_model,
        "target_context_bundle": context_bundle,
        "corpus_entry_sha256": _sha256(
            provenance.get("corpus_entry_sha256"), "corpus entry SHA-256"
        ),
        "source_scenario_sha256": _sha256(
            provenance.get("source_scenario_sha256"), "source scenario SHA-256"
        ),
        "catalog_sha256": catalog_sha,
    }
    try:
        normalized = normalize_runner_scenarios([candidate])[0]
    except Exception as error:
        raise ChronicleOld50DynamicV3CompilerError(
            f"dynamic-v3 runner rejected compiled scenario: {error}"
        ) from error
    core = {
        "schema": SCHEMA,
        "revision": REVISION,
        "status": STATUS,
        "source_bindings": scenario_model["source"],
        "hypothesis_selection": selection_receipt,
        "causal_background_projection": background_receipt,
        "scenario": normalized,
        "scientific_boundary": {
            "historical_truth": False,
            "diagnostic_nonvoting": True,
            "comparison_ready": False,
            "formal_superiority_claim_allowed": False,
            "future_background_schedule_visible_to_policy": False,
        },
    }
    artifact = deepcopy(core)
    artifact["content_address"] = {
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": sha256_json(core),
    }
    validate_old50_dynamic_v3_scenario_v1(artifact)
    return CompiledOld50DynamicV3ScenarioV1(
        artifact=artifact,
        scenario=normalized,
        dynamic_config=dynamic_config,
    )


def validate_old50_dynamic_v3_scenario_v1(value: Mapping[str, Any]) -> JSONMap:
    raw = _strict_json(value, "compiled old-50 dynamic-v3 artifact")
    expected_fields = {
        "schema",
        "revision",
        "status",
        "source_bindings",
        "hypothesis_selection",
        "causal_background_projection",
        "scenario",
        "scientific_boundary",
        "content_address",
    }
    if set(raw) != expected_fields or (
        raw.get("schema") != SCHEMA
        or raw.get("revision") != REVISION
        or raw.get("status") != STATUS
    ):
        raise ChronicleOld50DynamicV3CompilerError(
            "compiled artifact schema or field set differs"
        )
    address = _mapping(raw.get("content_address"), "content_address")
    core = {key: item for key, item in raw.items() if key != "content_address"}
    if address != {
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": sha256_json(core),
    }:
        raise ChronicleOld50DynamicV3CompilerError(
            "compiled artifact content address differs"
        )
    boundary = _mapping(raw.get("scientific_boundary"), "scientific_boundary")
    if boundary != {
        "historical_truth": False,
        "diagnostic_nonvoting": True,
        "comparison_ready": False,
        "formal_superiority_claim_allowed": False,
        "future_background_schedule_visible_to_policy": False,
    }:
        raise ChronicleOld50DynamicV3CompilerError(
            "compiled artifact scientific boundary widened"
        )
    scenario = _mapping(raw.get("scenario"), "scenario")
    try:
        normalized = normalize_runner_scenarios([scenario])[0]
    except Exception as error:
        raise ChronicleOld50DynamicV3CompilerError(
            f"compiled scenario no longer satisfies runner v4: {error}"
        ) from error
    if normalized != scenario:
        raise ChronicleOld50DynamicV3CompilerError(
            "compiled scenario is not stored in canonical runner form"
        )
    if (
        _mapping(scenario.get("scenario_model"), "scenario_model").get(
            "comparison_eligible"
        )
        is not False
        or _mapping(
            scenario.get("target_context_bundle"), "target_context_bundle"
        ).get("comparison_eligible")
        is not False
    ):
        raise ChronicleOld50DynamicV3CompilerError(
            "compiled scenario became comparison eligible"
        )
    return raw


__all__ = (
    "CompiledOld50DynamicV3ScenarioV1",
    "ChronicleOld50DynamicV3CompilerError",
    "LIMITATION_CODES",
    "REVISION",
    "SCHEMA",
    "STATUS",
    "compile_old50_dynamic_v3_scenario_v1",
    "validate_old50_dynamic_v3_scenario_v1",
)
