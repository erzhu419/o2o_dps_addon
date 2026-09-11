"""Auditable Cat2 ActionPlan IR for later, separately gated Lua distillation.

The compiler consumes the exact Cat2 capability manifest, never executes Lua,
and emits only a plan.  Traversal control, sink attempts, client acceptance,
and server outcomes remain separate concepts.  Only source paths whose
semantics were explicitly audited are supported; all other cards fail closed.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence

from .cat2_capability_manifest_v1 import (
    DEFAULT_MANIFEST,
    EXPECTED_MANIFEST_SHA256,
    Cat2CapabilityManifestError,
    load_manifest,
    verify_source_tree,
)


REQUEST_SCHEMA = "cat2_action_plan_request/v1"
PLAN_SCHEMA = "cat2_action_plan/v1"
CONTENT_ADDRESS_ALGORITHM = "sha256-canonical-json-v1"
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")

# These are the exact effective numeric domains in the pinned cards' source
# optionSchema.  Cat2 accepts looser persisted inputs and normalizes them at
# runtime; the plan format deliberately accepts only already-normalized values
# so a content address cannot hide string conversion, rounding, or clamping.
_OPTION_SPECS: dict[str, dict[str, dict[str, int | float | bool]]] = {
    "common_flow_terminate": {},
    "warrior_bloodrage": {
        "maximumRage": {"minimum": 1, "maximum": 100, "integer": True},
    },
    "warrior_charge_auto_stance": {
        "maximumRage": {"minimum": 0, "maximum": 100, "integer": True},
    },
    "warrior_cleave": {
        "rageThreshold": {"minimum": 1, "maximum": 100, "integer": True},
    },
    "warrior_cleave_front_only": {},
    "warrior_execute": {},
    "warrior_execute_high_rage": {
        "minimumRage": {"minimum": 1, "maximum": 100, "integer": True},
    },
    "warrior_execute_nearby_target": {},
    "warrior_heroic_strike": {
        "rageThreshold": {"minimum": 1, "maximum": 100, "integer": True},
    },
    "warrior_heroic_strike_alt": {
        "rageThreshold": {"minimum": 1, "maximum": 100, "integer": True},
    },
    "warrior_intercept_auto_stance": {
        "maximumRage": {"minimum": 10, "maximum": 100, "integer": True},
    },
    "warrior_intervene_auto_stance": {
        "maximumRage": {"minimum": 10, "maximum": 100, "integer": True},
    },
    "warrior_overpower_after_main_skill": {
        "maximumRage": {"minimum": 1, "maximum": 100, "integer": True},
        "mainSkillCooldownThreshold": {
            "minimum": 0,
            "maximum": 10,
            "integer": False,
        },
    },
    "warrior_pummel_flurry": {
        "rageThreshold": {"minimum": 10, "maximum": 100, "integer": True},
    },
    "warrior_shield_block": {
        "triggerPercent": {"minimum": 1, "maximum": 100, "integer": True},
    },
    "warrior_shield_wall_auto_stance": {
        "triggerPercent": {"minimum": 1, "maximum": 99, "integer": True},
    },
    "warrior_slam_after_main_skills": {
        "mainSkillCooldownThreshold": {
            "minimum": 0,
            "maximum": 10,
            "integer": False,
        },
        "minimumSwingTime": {
            "minimum": 0.1,
            "maximum": 5,
            "integer": False,
        },
        "rageThreshold": {"minimum": 15, "maximum": 100, "integer": True},
    },
    "warrior_sunder_armor_boss": {
        "rageThreshold": {"minimum": 5, "maximum": 100, "integer": True},
    },
    "warrior_sunder_armor_five_boss": {
        "rageThreshold": {"minimum": 5, "maximum": 100, "integer": True},
    },
    "warrior_thunder_clap_offensive": {
        "rageThreshold": {"minimum": 16, "maximum": 100, "integer": True},
        "minimumEnemies": {"minimum": 1, "maximum": 20, "integer": True},
    },
}

# Option keys are part of a profile step.  The direct helper omits that step,
# so every card with a non-empty tuple is rejected in DIRECT_CARD mode.
SUPPORTED_CARD_OPTIONS: dict[str, tuple[str, ...]] = {
    card_id: tuple(specs) for card_id, specs in _OPTION_SPECS.items()
}

_SIMPLE_CASTS: dict[str, tuple[str, bool]] = {
    "warrior_bloodrage": ("血性狂暴", False),
    "warrior_cleave": ("顺劈斩", False),
    "warrior_heroic_strike": ("英勇打击", False),
    "warrior_pummel_flurry": ("拳击", True),
    "warrior_shield_block": ("盾牌格挡", False),
    "warrior_slam_after_main_skills": ("猛击", True),
    "warrior_sunder_armor_boss": ("破甲攻击", True),
    "warrior_sunder_armor_five_boss": ("破甲攻击", True),
    "warrior_thunder_clap_offensive": ("雷霆一击", True),
}
_EXECUTE_PATH_CARDS = {"warrior_execute", "warrior_execute_high_rage"}
_AUTO_STANCE_CARDS = {
    "warrior_charge_auto_stance",
    "warrior_intercept_auto_stance",
    "warrior_intervene_auto_stance",
    "warrior_shield_wall_auto_stance",
}
_PASSIVE_CARDS = {"warrior_cleave_front_only"}
_AUTO_STANCE_SPELLS: dict[str, tuple[str, str]] = {
    "warrior_charge_auto_stance": ("战斗姿态", "冲锋"),
    "warrior_intercept_auto_stance": ("狂暴姿态", "拦截"),
    "warrior_intervene_auto_stance": ("防御姿态", "援护"),
    "warrior_shield_wall_auto_stance": ("防御姿态", "盾墙"),
}


class Cat2ActionPlanError(ValueError):
    """The request or ActionPlan violates the audited contract."""


def _fail(path: str, message: str) -> Cat2ActionPlanError:
    return Cat2ActionPlanError(f"{path}: {message}")


def _object(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _fail(path, f"expected object, got {type(value).__name__}")
    return value


def _array(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise _fail(path, f"expected array, got {type(value).__name__}")
    return value


def _keys(value: Mapping[str, Any], expected: set[str], path: str) -> None:
    actual = set(value)
    if actual != expected:
        raise _fail(
            path,
            f"key mismatch; missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}",
        )


def _identifier(value: Any, path: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise _fail(path, "expected 1-128 characters from [A-Za-z0-9._-]")
    return value


def _effective_option_value(
    value: Any,
    spec: Mapping[str, int | float | bool],
    path: str,
) -> int | float:
    if spec["integer"]:
        if type(value) is not int:
            raise _fail(path, "expected a canonical integer option value")
        normalized: int | float = value
    else:
        if type(value) not in {int, float} or not math.isfinite(value):
            raise _fail(path, "expected a finite numeric option value")
        normalized = float(value)
    minimum = spec["minimum"]
    maximum = spec["maximum"]
    if normalized < minimum or normalized > maximum:
        raise _fail(path, f"expected value in inclusive range [{minimum}, {maximum}]")
    return normalized


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise _fail("plan", f"not canonical JSON: {error}") from error


def _content_address(core: Mapping[str, Any]) -> dict[str, str]:
    return {
        "algorithm": CONTENT_ADDRESS_ALGORITHM,
        "scope": "canonical JSON document excluding content_address",
        "sha256": hashlib.sha256(_canonical_bytes(core)).hexdigest(),
    }


def _path_map(manifest: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    paths = manifest["execution_contract"]["multi_sink_paths"]
    return {item["path_id"]: item for item in paths}


def _ordered_operations(
    path: Mapping[str, Any],
    arguments_by_order: Mapping[int, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for expected_order, raw in enumerate(path["ordered_sinks"], start=1):
        if raw["order"] != expected_order:
            raise _fail(
                f"manifest.execution_contract.{path['path_id']}",
                "sink order is not contiguous",
            )
        operation = {
            "order": expected_order,
            "kind": "SINK_OPERATION",
            "operation": raw["sink"],
            "cardinality": raw["cardinality"],
            "condition": raw["condition"],
            "observation": "PLANNED_NOT_OBSERVED",
        }
        if arguments_by_order and expected_order in arguments_by_order:
            operation["arguments"] = deepcopy(arguments_by_order[expected_order])
        operations.append(operation)
    return operations


def _traversal(*, stops_on_operation_path: bool) -> dict[str, str]:
    return {
        "return_signal_on_operation_path": (
            "LITERAL_TRUE" if stops_on_operation_path else "LITERAL_FALSE_OR_NIL"
        ),
        "effect_on_operation_path": (
            "STOP_CURRENT_CONFIGURATION_PASS_ONLY"
            if stops_on_operation_path
            else "CONTINUE_CURRENT_CONFIGURATION_PASS"
        ),
        "return_signal_on_no_match": "LITERAL_FALSE_OR_NIL",
        "effect_on_no_match": "CONTINUE_CURRENT_CONFIGURATION_PASS",
    }


def _manifest_path_ir(
    manifest: Mapping[str, Any],
    path_id: str,
    arguments_by_order: Mapping[int, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    try:
        source_path = _path_map(manifest)[path_id]
    except KeyError as error:
        raise _fail("manifest.execution_contract", f"missing path {path_id!r}") from error
    operations = _ordered_operations(source_path, arguments_by_order)
    common = {
        "semantic_path_id": path_id,
        "scope": source_path["scope"],
        "repeat": source_path["repeat"],
    }
    if path_id == "nearby_execute_loop":
        return {
            **common,
            "kind": "FOR_EACH_TARGET",
            "selector": "Cat2.ScanNearbyEnemies",
            "target_cardinality": {"minimum": 0, "maximum": None},
            "iteration_order": {
                "kind": "UNSPECIFIED",
                "source": "LUA_PAIRS",
                "must_not_sort_or_flatten": True,
            },
            "body": {
                "kind": "ORDERED_SINK_SEQUENCE",
                "operations": operations,
                "internal_bookkeeping": [
                    {
                        "operation": "Cat2.RecordPendingCastTarget",
                        "classification": "NON_ACTION_INTERNAL_EFFECT",
                        "timing": "AFTER_SINK_ORDER_1_BEFORE_SINK_ORDER_2",
                        "arguments": {
                            "spell": "斩杀",
                            "unit": "LOOP_TARGET_UNIT",
                        },
                        "condition": "function is available",
                    }
                ],
            },
            "traversal_after_loop": source_path["traversal_after_path"],
        }
    if path_id == "auto_stance_multi_press":
        return {
            **common,
            "kind": "STATE_GATED_MULTI_PRESS_SEQUENCE",
            "atomic": False,
            "ordered_stages": operations,
            "entry_state_branches": [
                {
                    "condition": "REQUIRED_STANCE_ABSENT",
                    "stage_on_this_press": 1,
                    "effect": "CAST_STANCE_AND_STOP_THIS_PASS",
                    "ability_requires_later_matching_press": True,
                },
                {
                    "condition": "REQUIRED_STANCE_PRESENT",
                    "stage_on_this_press": 2,
                    "effect": "CAST_ABILITY_AND_STOP_THIS_PASS",
                    "ability_may_run_on_first_matching_press": True,
                },
            ],
            "stage_order_constraint": (
                "STANCE_PRECEDES_ABILITY_ONLY_WHEN_A_STANCE_CHANGE_IS_REQUIRED"
            ),
            "traversal_after_each_stage": source_path["traversal_after_path"],
        }
    result = {
        **common,
        "kind": "ORDERED_SINK_SEQUENCE",
        "operations": operations,
        "traversal_after_sequence": source_path["traversal_after_path"],
    }
    if path_id == "pending_interrupt_prepass":
        result["internal_bookkeeping"] = [
            {
                "operation": "Cat2.DispatchCardInternalEvent",
                "classification": "NON_ACTION_INTERNAL_EFFECT",
                "timing": "BEFORE_SINK_ORDER_1",
                "arguments": {"event": "CAT2_CAST_INTERRUPTED"},
                "condition": "interrupt information and dispatcher are available",
            },
            {
                "operation": "StopMonitoring",
                "classification": "NON_ACTION_INTERNAL_EFFECT",
                "timing": "AFTER_SINK_ORDER_1",
                "condition": "pending interrupt sink path runs",
            },
        ]
    return result


def _conditional_cast(spell: str) -> dict[str, Any]:
    return {
        "kind": "CONDITIONAL_SINK_SEQUENCE",
        "operations": [
            {
                "order": 1,
                "kind": "SINK_OPERATION",
                "operation": "Cat2.Cast",
                "arguments": {"spell": spell},
                "cardinality": "0..1_PER_CARD_INVOCATION",
                "condition": "card source gates match",
                "downstream_client_sink": "MAY_BE_SUPPRESSED_WHILE_CHANNELING",
                "observation": "PLANNED_NOT_OBSERVED",
            }
        ],
    }


def _exclusive_cast_branch(
    branches: Sequence[tuple[str, str]]
) -> dict[str, Any]:
    return {
        "kind": "ORDERED_EXCLUSIVE_BRANCH",
        "branch_order": "SOURCE_BRANCH_ORDER",
        "branches": [
            {
                "order": order,
                "condition": condition,
                "operations": [
                    {
                        "order": 1,
                        "kind": "SINK_OPERATION",
                        "operation": "Cat2.Cast",
                        "arguments": {"spell": spell},
                        "cardinality": "0..1_PER_CARD_INVOCATION",
                        "downstream_client_sink": "MAY_BE_SUPPRESSED_WHILE_CHANNELING",
                        "observation": "PLANNED_NOT_OBSERVED",
                    }
                ],
            }
            for order, (condition, spell) in enumerate(branches, start=1)
        ],
    }


def _compile_card_ir(
    card_id: str, manifest: Mapping[str, Any]
) -> tuple[str, dict[str, Any], dict[str, str]]:
    if card_id == "common_flow_terminate":
        return "active", {"kind": "NO_SINK", "operations": []}, _traversal(
            stops_on_operation_path=True
        )
    if card_id in _PASSIVE_CARDS:
        return (
            "passive",
            {
                "kind": "PASSIVE_CONTEXT_MUTATION",
                "writes": ["context.parameters.warriorCleaveFrontOnly=true"],
                "operations": [],
            },
            {
                "return_signal_on_operation_path": "VALIDATE_LITERAL_TRUE",
                "effect_on_operation_path": "ALLOW_ACTIVE_PHASE",
                "return_signal_on_no_match": "NOT_APPLICABLE",
                "effect_on_no_match": "NOT_APPLICABLE",
            },
        )
    if card_id in _EXECUTE_PATH_CARDS:
        return (
            "active",
            _manifest_path_ir(
                manifest,
                "execute_interrupt_then_cast",
                {2: {"spell": "斩杀"}},
            ),
            _traversal(stops_on_operation_path=True),
        )
    if card_id == "warrior_execute_nearby_target":
        return (
            "active",
            _manifest_path_ir(
                manifest,
                "nearby_execute_loop",
                {
                    2: {"unit": "LOOP_TARGET_UNIT"},
                    3: {"spell": "斩杀"},
                    4: {"saved_target": "PRE_HELPER_CURRENT_TARGET_OR_CLEAR"},
                },
            ),
            _traversal(stops_on_operation_path=False),
        )
    if card_id in _AUTO_STANCE_CARDS:
        stance_spell, ability_spell = _AUTO_STANCE_SPELLS[card_id]
        return (
            "active",
            _manifest_path_ir(
                manifest,
                "auto_stance_multi_press",
                {
                    1: {"spell": stance_spell},
                    2: {"spell": ability_spell},
                },
            ),
            _traversal(stops_on_operation_path=True),
        )
    if card_id == "warrior_heroic_strike_alt":
        return (
            "active",
            _exclusive_cast_branch(
                (
                    ("eligible nearby enemy count is at least two", "顺劈斩"),
                    ("eligible nearby enemy count is below two", "英勇打击"),
                )
            ),
            _traversal(stops_on_operation_path=False),
        )
    if card_id == "warrior_overpower_after_main_skill":
        return (
            "active",
            _exclusive_cast_branch(
                (
                    ("battle stance and Overpower gates match", "压制"),
                    ("stance transition gates match", "战斗姿态"),
                )
            ),
            _traversal(stops_on_operation_path=True),
        )
    if card_id in _SIMPLE_CASTS:
        spell, stops = _SIMPLE_CASTS[card_id]
        return "active", _conditional_cast(spell), _traversal(
            stops_on_operation_path=stops
        )
    raise _fail(
        "request.invocations.card_id",
        f"card {card_id!r} has no audited ActionPlan template",
    )


def _normalize_request(request: Any) -> dict[str, Any]:
    root = _object(request, "request")
    _keys(root, {"schema", "plan_name", "entry_mode", "invocations"}, "request")
    if root["schema"] != REQUEST_SCHEMA:
        raise _fail("request.schema", f"expected {REQUEST_SCHEMA!r}")
    plan_name = _identifier(root["plan_name"], "request.plan_name")
    entry_mode = root["entry_mode"]
    if entry_mode not in {"PROFILE", "DIRECT_CARD"}:
        raise _fail("request.entry_mode", "expected PROFILE or DIRECT_CARD")
    raw_invocations = _array(root["invocations"], "request.invocations")
    if not raw_invocations:
        raise _fail("request.invocations", "at least one invocation is required")
    if len(raw_invocations) > 128:
        raise _fail("request.invocations", "at most 128 invocations are allowed")
    if entry_mode == "DIRECT_CARD" and len(raw_invocations) != 1:
        raise _fail("request.invocations", "DIRECT_CARD requires exactly one invocation")

    seen_invocations: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_invocations):
        path = f"request.invocations[{index}]"
        invocation = _object(raw, path)
        _keys(invocation, {"invocation_id", "card_id", "step"}, path)
        invocation_id = _identifier(invocation["invocation_id"], f"{path}.invocation_id")
        if invocation_id in seen_invocations:
            raise _fail(f"{path}.invocation_id", "duplicate invocation ID")
        seen_invocations.add(invocation_id)
        card_id = invocation["card_id"]
        if card_id not in SUPPORTED_CARD_OPTIONS:
            raise _fail(f"{path}.card_id", f"unsupported or unaudited card {card_id!r}")

        option_keys = SUPPORTED_CARD_OPTIONS[card_id]
        if entry_mode == "DIRECT_CARD":
            if invocation["step"] is not None:
                raise _fail(
                    f"{path}.step",
                    "Cat2.ExecuteCardById does not pass a step argument",
                )
            if card_id in _PASSIVE_CARDS:
                raise _fail(
                    f"{path}.card_id",
                    "direct passive-card execution is unsafe because "
                    "Cat2.ExecuteCardById does not call Apply or Validate",
                )
            if option_keys:
                raise _fail(
                    f"{path}.card_id",
                    "direct optioned-card execution is unsafe because "
                    "Cat2.ExecuteCardById omits the step argument",
                )
            step = None
        else:
            step_raw = _object(invocation["step"], f"{path}.step")
            _keys(step_raw, {"option_values"}, f"{path}.step")
            raw_options = _object(step_raw["option_values"], f"{path}.step.option_values")
            unknown = sorted(set(raw_options) - set(option_keys))
            if unknown:
                raise _fail(
                    f"{path}.step.option_values",
                    f"unknown or unaudited option keys {unknown}",
                )
            options = {
                key: _effective_option_value(
                    value,
                    _OPTION_SPECS[card_id][key],
                    f"{path}.step.option_values.{key}",
                )
                for key, value in sorted(raw_options.items())
            }
            step = {"option_values": options}
        normalized.append(
            {
                "invocation_id": invocation_id,
                "card_id": card_id,
                "step": step,
            }
        )
    return {
        "schema": REQUEST_SCHEMA,
        "plan_name": plan_name,
        "entry_mode": entry_mode,
        "invocations": normalized,
    }


def _enforce_profile_groups(
    normalized: Mapping[str, Any], manifest: Mapping[str, Any]
) -> None:
    if normalized["entry_mode"] != "PROFILE":
        return
    ids = [item["card_id"] for item in normalized["invocations"]]
    passive = set(manifest["warrior_inventory"]["passive_card_ids"])
    for card_id in passive:
        if ids.count(card_id) > 1:
            raise _fail("request.invocations", f"unique passive {card_id!r} is duplicated")
    for group_name, members in manifest["warrior_inventory"]["exclusive_groups"].items():
        selected = [card_id for card_id in ids if card_id in members]
        if len(selected) > 1:
            raise _fail(
                "request.invocations",
                f"exclusive group {group_name!r} contains {selected}",
            )


def _build_core(request: Any, manifest: Mapping[str, Any]) -> dict[str, Any]:
    normalized = _normalize_request(request)
    _enforce_profile_groups(normalized, manifest)
    mode = normalized["entry_mode"]
    nodes: list[dict[str, Any]] = []
    passive_nodes: list[str] = []
    active_nodes: list[str] = []
    for ordinal, invocation in enumerate(normalized["invocations"], start=1):
        behavior, operations, traversal = _compile_card_ir(
            invocation["card_id"], manifest
        )
        if operations["kind"] == "NO_SINK":
            outcome_evidence = {
                "sink_attempt": "NO_SINK_BY_DESIGN",
                "client_acceptance": "NOT_APPLICABLE_NO_CLIENT_SINK",
                "server_outcome": "NOT_APPLICABLE_NO_CLIENT_SINK",
            }
        elif behavior == "passive":
            outcome_evidence = {
                "sink_attempt": "INTERNAL_MUTATION_NOT_CLIENT_SINK",
                "client_acceptance": "NOT_APPLICABLE_NO_CLIENT_SINK",
                "server_outcome": "NOT_APPLICABLE_NO_CLIENT_SINK",
            }
        else:
            outcome_evidence = {
                "sink_attempt": "PLANNED_NOT_OBSERVED",
                "client_acceptance": "UNKNOWN_NOT_INFERRED",
                "server_outcome": "UNKNOWN_NOT_INFERRED",
            }
        node = {
            "node_id": invocation["invocation_id"],
            "profile_ordinal": ordinal if mode == "PROFILE" else None,
            "kind": "CARD_INVOCATION",
            "card_id": invocation["card_id"],
            "behavior": behavior,
            "entry": (
                "Cat2.ExecuteConfiguration.PROFILE_STEP"
                if mode == "PROFILE"
                else "Cat2.ExecuteCardById"
            ),
            "step": deepcopy(invocation["step"]),
            "operations": operations,
            "traversal": traversal,
            "outcome_evidence": outcome_evidence,
        }
        nodes.append(node)
        (passive_nodes if behavior == "passive" else active_nodes).append(
            invocation["invocation_id"]
        )

    paths = _path_map(manifest)
    prepass = (
        _manifest_path_ir(manifest, "pending_interrupt_prepass")
        if mode == "PROFILE"
        else None
    )
    entry_contract = {
        "mode": mode,
        "call": (
            "Cat2.ExecuteConfiguration"
            if mode == "PROFILE"
            else "Cat2.ExecuteCardById"
        ),
        "passes_step_argument": mode == "PROFILE",
        "entry_prepass": prepass,
    }
    schedule = {
        "phase_order": (
            deepcopy(manifest["execution_contract"]["phase_order"])
            if mode == "PROFILE"
            else ["DIRECT_CARD_CALL"]
        ),
        "profile_step_order": [item["invocation_id"] for item in normalized["invocations"]],
        "passive_apply_validate_order": passive_nodes,
        "active_execution_order": active_nodes,
        "active_error_behavior": (
            manifest["execution_contract"]["active_error_behavior"]
            if mode == "PROFILE"
            else "PROPAGATE_DIRECT_CALL_ERROR"
        ),
        "nonblocking_chain_semantic_ref": (
            paths["profile_order_nonblocking_chain"]["path_id"]
            if mode == "PROFILE"
            else None
        ),
    }
    return {
        "schema": PLAN_SCHEMA,
        "request": normalized,
        "provenance": {
            "capability_manifest_id": manifest["manifest_id"],
            "capability_manifest_sha256": EXPECTED_MANIFEST_SHA256,
            "source_id": manifest["source"]["source_id"],
            "source_tree_sha256": manifest["identity"]["tree"]["sha256"],
            "capability_manifest_verification": "STRICT_LOAD_PASS",
            "source_tree_verification": "NOT_ATTESTED_IN_PLAN_REFERENCES_PIN_ONLY",
            "archive_status": manifest["source"]["archive"]["status"],
            "authority_role": "CAPABILITY_SOURCE",
            "deployed": False,
            "runtime_validated": False,
            "eligible_for_independent_vote": False,
            "known_boundary_ids": [
                item["id"] for item in manifest["known_boundaries"]
            ],
        },
        "entry_contract": entry_contract,
        "execution_schedule": schedule,
        "nodes": nodes,
        "evidence_contract": {
            "traversal_signal": "SOURCE_DERIVED_CONTROL_FLOW_ONLY",
            "sink_attempt": "PLANNED_NOT_OBSERVED",
            "client_acceptance": "UNKNOWN_NOT_INFERRED_FROM_TRAVERSAL_OR_SINK_CALL",
            "server_outcome": "UNKNOWN_REQUIRES_CAUSALLY_LINKED_RUNTIME_EVENT",
            "forbidden_collapses": [
                "TRAVERSAL_TRUE_TO_CLIENT_ACCEPTANCE",
                "SINK_ATTEMPT_TO_CLIENT_ACCEPTANCE",
                "CLIENT_ACCEPTANCE_TO_SERVER_OUTCOME",
                "ORDERED_MULTI_SINK_TO_SINGLE_CATEGORICAL_ACTION",
                "UNSPECIFIED_TARGET_ORDER_TO_SORTED_TARGET_ORDER",
                "INTERNAL_BOOKKEEPING_TO_ACTION_SINK",
            ],
        },
        "distillation_contract": {
            "target": "LUA_ACTION_PLAN_V1",
            "status": "PLAN_ONLY_NOT_DISTILLED",
            "preserve_operation_order": True,
            "preserve_loop_cardinality": True,
            "preserve_unspecified_iteration_order": True,
            "preserve_internal_bookkeeping_order": True,
            "internal_bookkeeping_operations_are_action_sinks": False,
            "flatten_multi_sink_operations": False,
            "source_tree_verification_required": True,
            "runtime_validation_required_after_distillation": True,
            "deployment_authorized": False,
        },
    }


def build_action_plan(
    request: Any, manifest_path: str | Path = DEFAULT_MANIFEST
) -> dict[str, Any]:
    """Compile a request into a deterministic content-addressed plan."""

    manifest = load_manifest(manifest_path)
    core = _build_core(request, manifest)
    return {**core, "content_address": _content_address(core)}


def validate_action_plan(
    plan: Any, manifest_path: str | Path = DEFAULT_MANIFEST
) -> dict[str, Any]:
    """Verify content address and deterministic recompilation from the request."""

    document = _object(plan, "plan")
    expected_keys = {
        "schema",
        "request",
        "provenance",
        "entry_contract",
        "execution_schedule",
        "nodes",
        "evidence_contract",
        "distillation_contract",
        "content_address",
    }
    _keys(document, expected_keys, "plan")
    if document["schema"] != PLAN_SCHEMA:
        raise _fail("plan.schema", f"expected {PLAN_SCHEMA!r}")
    address = _object(document["content_address"], "plan.content_address")
    _keys(address, {"algorithm", "scope", "sha256"}, "plan.content_address")
    core = {key: deepcopy(value) for key, value in document.items() if key != "content_address"}
    expected_address = _content_address(core)
    if dict(address) != expected_address:
        raise _fail("plan.content_address", "content hash mismatch")

    rebuilt = build_action_plan(document["request"], manifest_path)
    if rebuilt != dict(document):
        raise _fail("plan", "does not match deterministic compilation of its request")
    return dict(document)


def serialize_action_plan(
    plan: Any, manifest_path: str | Path = DEFAULT_MANIFEST
) -> bytes:
    """Return compact UTF-8 JSON plus one newline after strict validation."""

    validated = validate_action_plan(plan, manifest_path)
    return _canonical_bytes(validated) + b"\n"


def _load_request(path: str | Path) -> Any:
    request_path = Path(path).expanduser().resolve(strict=True)
    try:
        return json.loads(
            request_path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON constant {value}")
            ),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise _fail("request", f"cannot load strict JSON: {error}") from error


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compile a plan-only Cat2 ActionPlan IR to stdout."
    )
    parser.add_argument("--request", required=True, help="ActionPlan request JSON")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument(
        "--cat2-root",
        help="optionally verify exact external Cat2_new source bytes before planning",
    )
    args = parser.parse_args(argv)
    try:
        if args.cat2_root:
            verify_source_tree(args.cat2_root, args.manifest)
        plan = build_action_plan(_load_request(args.request), args.manifest)
        sys.stdout.buffer.write(serialize_action_plan(plan, args.manifest))
    except (Cat2ActionPlanError, Cat2CapabilityManifestError, OSError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
