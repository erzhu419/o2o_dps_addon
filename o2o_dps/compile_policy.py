"""Compile a bounded JSON policy into a legacy-WoW-compatible Lua resolver."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any, Sequence


SCHEMA_VERSION = 1
POLICY_ID = re.compile(r"^[a-z][a-z0-9_]*$")
CARD_ID = re.compile(r"^[a-z][a-z0-9_]*$")
STATE_FIELDS = {
    "classFile",
    "level",
    "inCombat",
    "health",
    "maximumHealth",
    "percentHealth",
    "power",
    "maximumPower",
    "powerType",
    "rage",
    "maximumRage",
    "energy",
    "maximumEnergy",
    "mana",
    "maximumMana",
    "gcd",
    "bloodrageCooldown",
    "bloodrageCooldownKnown",
    "bloodthirstCooldown",
    "bloodthirstCooldownKnown",
    "whirlwindCooldown",
    "whirlwindCooldownKnown",
    "mainHandSwingRemaining",
    "mainHandSwingRemainingKnown",
    "queuedSwing",
    "queuedSwingKnown",
    "buffCount",
    "targetExists",
    "targetLevel",
    "targetLevelKnown",
    "targetCanAttack",
    "targetIsDead",
    "targetInCombat",
    "targetHealth",
    "targetPercentHealth",
    "targetIsBoss",
    "targetBuffCount",
    "nearbyEnemies",
    "nearbyEnemiesKnown",
}
OPERATORS = {
    "eq": "==",
    "ne": "~=",
    "lt": "<",
    "le": "<=",
    "gt": ">",
    "ge": ">=",
}
KNOWN_FIELD_BY_VALUE_FIELD = {
    "bloodrageCooldown": "bloodrageCooldownKnown",
    "bloodthirstCooldown": "bloodthirstCooldownKnown",
    "whirlwindCooldown": "whirlwindCooldownKnown",
    "mainHandSwingRemaining": "mainHandSwingRemainingKnown",
    "queuedSwing": "queuedSwingKnown",
    "targetLevel": "targetLevelKnown",
}


class PolicyCompileError(ValueError):
    """The policy cannot be represented by the in-game resolver."""


def _lua_literal(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "nil"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return repr(value)
    if isinstance(value, str):
        if "\n" in value or "\r" in value:
            raise PolicyCompileError("string condition values must be single-line")
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    raise PolicyCompileError(f"unsupported condition value: {value!r}")


def _conditions(raw: Any, location: str) -> list[tuple[str, str, Any]]:
    if not isinstance(raw, list):
        raise PolicyCompileError(f"{location} must be a list")
    result: list[tuple[str, str, Any]] = []
    for index, condition in enumerate(raw):
        item_location = f"{location}[{index}]"
        if not isinstance(condition, dict):
            raise PolicyCompileError(f"{item_location} must be an object")
        field = condition.get("field")
        operation = condition.get("op")
        if field not in STATE_FIELDS:
            raise PolicyCompileError(f"{item_location}.field is not exported: {field!r}")
        if operation not in OPERATORS:
            raise PolicyCompileError(f"{item_location}.op is unsupported: {operation!r}")
        if "value" not in condition:
            raise PolicyCompileError(f"{item_location}.value is required")
        value = condition["value"]
        _lua_literal(value)
        result.append((field, operation, value))
    return result


def _actions(raw: Any, location: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PolicyCompileError(f"{location} must be a list")
    result: list[str] = []
    for index, card_id in enumerate(raw):
        if not isinstance(card_id, str) or CARD_ID.fullmatch(card_id) is None:
            raise PolicyCompileError(f"{location}[{index}] is not a Cat2 card id")
        result.append(card_id)
    return result


def _condition_expression(conditions: list[tuple[str, str, Any]]) -> str:
    if not conditions:
        return "true"
    expressions: list[str] = []
    for field, operation, value in conditions:
        comparison = (
            f"state.{field} {OPERATORS[operation]} {_lua_literal(value)}"
        )
        known_field = KNOWN_FIELD_BY_VALUE_FIELD.get(field)
        if known_field:
            comparison = f"(state.{known_field} == true and {comparison})"
        expressions.append(comparison)
    return " and ".join(expressions)


def _emit_actions(lines: list[str], lane: str, actions: list[str], indent: str) -> None:
    for card_id in actions:
        lines.append(f'{indent}table.insert(proposal.{lane}, "{card_id}")')


def compile_policy(document: dict[str, Any], *, source_name: str = "policy.json") -> str:
    if not isinstance(document, dict):
        raise PolicyCompileError("policy document must be an object")
    if document.get("schema_version") != SCHEMA_VERSION:
        raise PolicyCompileError(f"schema_version must be {SCHEMA_VERSION}")

    policy_id = document.get("policy_id")
    if not isinstance(policy_id, str) or POLICY_ID.fullmatch(policy_id) is None:
        raise PolicyCompileError("policy_id must match ^[a-z][a-z0-9_]*$")
    policy_kind = document.get("policy_kind")
    if not isinstance(policy_kind, str) or not policy_kind.strip():
        raise PolicyCompileError("policy_kind must be a non-empty string")
    activate = document.get("activate", False)
    if not isinstance(activate, bool):
        raise PolicyCompileError("activate must be boolean")

    guards = _conditions(document.get("guards", []), "guards")
    raw_prelude = document.get("prelude", {})
    if not isinstance(raw_prelude, dict):
        raise PolicyCompileError("prelude must be an object")
    prelude_off_gcd = _actions(raw_prelude.get("off_gcd"), "prelude.off_gcd")
    raw_rules = document.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise PolicyCompileError("rules must be a non-empty list")

    rules: list[
        tuple[
            list[tuple[str, str, Any]],
            list[str],
            list[str],
            list[str],
            bool,
        ]
    ] = []
    for index, raw_rule in enumerate(raw_rules):
        location = f"rules[{index}]"
        if not isinstance(raw_rule, dict):
            raise PolicyCompileError(f"{location} must be an object")
        when = _conditions(raw_rule.get("when", []), f"{location}.when")
        off_gcd = _actions(raw_rule.get("off_gcd"), f"{location}.off_gcd")
        queue = _actions(raw_rule.get("queue"), f"{location}.queue")
        gcd = _actions(raw_rule.get("gcd"), f"{location}.gcd")
        continue_evaluation = raw_rule.get("continue", False)
        if not isinstance(continue_evaluation, bool):
            raise PolicyCompileError(f"{location}.continue must be boolean")
        if not off_gcd and not queue and not gcd:
            raise PolicyCompileError(f"{location} has no actions")
        rules.append((when, off_gcd, queue, gcd, continue_evaluation))

    raw_default = document.get("default", {})
    if not isinstance(raw_default, dict):
        raise PolicyCompileError("default must be an object")
    default_off_gcd = _actions(raw_default.get("off_gcd"), "default.off_gcd")
    default_queue = _actions(raw_default.get("queue"), "default.queue")
    default_gcd = _actions(raw_default.get("gcd"), "default.gcd")

    clean_source = Path(source_name).name.replace("\n", " ").replace("\r", " ")
    lines = [
        "-- Generated by o2o_dps.compile_policy; edit the JSON source, not this file.",
        f"-- Source: {clean_source}",
        "local brain = BrainOfCat.PolicyBrain",
        f'local policyId = "{policy_id}"',
        "",
        "local function Resolve(state)",
        "    local proposal = {",
        f"        policyKind = {_lua_literal(policy_kind)},",
        "        off_gcd = {},",
        "        queue = {},",
        "        gcd = {},",
        "    }",
        "    if type(state) ~= \"table\" then",
        "        return proposal",
        "    end",
    ]
    if guards:
        lines.extend(
            [
                f"    if not ({_condition_expression(guards)}) then",
                "        return proposal",
                "    end",
            ]
        )

    _emit_actions(lines, "off_gcd", prelude_off_gcd, "    ")

    for when, off_gcd, queue, gcd, continue_evaluation in rules:
        lines.append(f"    if {_condition_expression(when)} then")
        _emit_actions(lines, "off_gcd", off_gcd, "        ")
        _emit_actions(lines, "queue", queue, "        ")
        _emit_actions(lines, "gcd", gcd, "        ")
        if not continue_evaluation:
            lines.append("        return proposal")
        lines.append("    end")

    _emit_actions(lines, "off_gcd", default_off_gcd, "    ")
    _emit_actions(lines, "queue", default_queue, "    ")
    _emit_actions(lines, "gcd", default_gcd, "    ")
    lines.extend(
        [
            "    return proposal",
            "end",
            "",
            "brain.RegisterPolicy(policyId, Resolve)",
        ]
    )
    if activate:
        lines.append("brain.FuryPolicyId = policyId")
    lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("policy", type=Path, help="input JSON policy")
    parser.add_argument("output", type=Path, help="output Lua file")
    args = parser.parse_args(argv)
    try:
        document = json.loads(args.policy.read_text(encoding="utf-8"))
        compiled = compile_policy(document, source_name=args.policy.name)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(compiled, encoding="utf-8", newline="\n")
    except (OSError, json.JSONDecodeError, PolicyCompileError) as error:
        print(f"policy compile failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps({"status": "ok", "policy": str(args.policy), "lua": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
