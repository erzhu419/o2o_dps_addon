"""Import one explicitly selected BrainOfCat.lua SavedVariables file.

The parser implements the literal Lua subset emitted by WoW SavedVariables:
top-level assignments, tables, string/integer keys, strings, numbers, booleans,
and nil. It never executes Lua and never searches for SavedVariables files.

Windows example::

    py -m o2o_dps.import_savedvariables \
      "D:\\World of Warcraft\\WTF\\Account\\ACCOUNT\\Realm\\Character\\SavedVariables\\BrainOfCat.lua"
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from typing import Any, Sequence


SOURCE_FORMAT = "wow_savedvariables_lua"
SCHEMA_VERSION = 1
SUPPORTED_SHADOW_CONTRACT_VERSIONS = frozenset({2, 3, 4})
DEFAULT_DATA_ROOT = Path(__file__).resolve().parents[1] / "offline_data"
_NUMBER_LITERAL = re.compile(r"[+-]?(?:(?:\d+(?:\.\d*)?)|(?:\.\d+))(?:[eE][+-]?\d+)?")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_EMPTY_ARRAY_FIELDS = {
    "entries",
    "off_gcd",
    "queue",
    "gcd",
    "attempts",
    "actions",
    "sinkActions",
    "serverObservedActions",
    "expertTraceLinks",
    "expectedSpellIds",
    "eventSequences",
    "compactEvents",
    "missingCVars",
    "playerAuras",
    "targetAuras",
    "equipment",
    "talents",
    "spellbook",
    "actionBarSpells",
    "skillLines",
    "bagItems",
    "attributes",
}
_TASK_INDEX_MAP_FIELDS = {
    "completedTaskIds",
    "incompleteTaskIds",
    "deferredTaskIds",
}


class SavedVariablesImportError(ValueError):
    """Base error for invalid or unreadable SavedVariables input."""


class SavedVariablesSyntaxError(SavedVariablesImportError):
    """The file is outside the supported WoW SavedVariables Lua subset."""


class SavedVariablesSchemaError(SavedVariablesImportError):
    """The parsed BrainOfCatCharacterDB does not match supported schema v1/v2."""


@dataclass(frozen=True)
class _Token:
    kind: str
    value: Any
    line: int
    column: int


@dataclass
class _LuaTable:
    fields: dict[str | int, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SavedVariablesImportResult:
    decision_count: int
    calibration_count: int
    expert_trace_count: int
    raw_copy: Path
    online_decisions: Path
    calibration: Path
    expert_traces: Path
    metadata: Path

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "decision_count": self.decision_count,
            "calibration_count": self.calibration_count,
            "expert_trace_count": self.expert_trace_count,
            "raw_copy": str(self.raw_copy),
            "online_decisions": str(self.online_decisions),
            "calibration": str(self.calibration),
            "expert_traces": str(self.expert_traces),
            "metadata": str(self.metadata),
        }


@dataclass(frozen=True)
class ShadowPairImportResult:
    """Receipt for the compact, append-only schema-v2 Shadow pair journal."""

    source_pair_total: int
    new_pair_count: int
    journal_pair_total: int
    output: Path
    manifest: Path

    def as_dict(self) -> dict[str, Any]:
        if self.new_pair_count:
            status = "imported"
        elif self.source_pair_total:
            status = "already_imported"
        else:
            status = "no_pairs"
        return {
            "status": status,
            "source_pair_total": self.source_pair_total,
            "new_pair_count": self.new_pair_count,
            "journal_pair_total": self.journal_pair_total,
            "output": str(self.output),
            "manifest": str(self.manifest),
        }


class _Lexer:
    def __init__(self, text: str, source_name: str) -> None:
        self.text = text
        self.source_name = source_name
        self.index = 0
        self.line = 1
        self.column = 1

    def _peek(self, offset: int = 0) -> str:
        index = self.index + offset
        if index >= len(self.text):
            return ""
        return self.text[index]

    def _advance(self) -> str:
        character = self._peek()
        if not character:
            return ""
        self.index += 1
        if character == "\n":
            self.line += 1
            self.column = 1
        else:
            self.column += 1
        return character

    def _error(
        self, message: str, line: int | None = None, column: int | None = None
    ) -> SavedVariablesSyntaxError:
        return SavedVariablesSyntaxError(
            f"{self.source_name}:{line or self.line}:{column or self.column}: {message}"
        )

    def _skip_space_and_comments(self) -> None:
        while True:
            while self._peek() and self._peek().isspace():
                self._advance()
            if self._peek() == "-" and self._peek(1) == "-":
                self._advance()
                self._advance()
                while self._peek() and self._peek() not in "\r\n":
                    self._advance()
                continue
            return

    def _read_string(self) -> _Token:
        line, column = self.line, self.column
        quote = self._advance()
        characters: list[str] = []
        escape_map = {
            "a": "\a",
            "b": "\b",
            "f": "\f",
            "n": "\n",
            "r": "\r",
            "t": "\t",
            "v": "\v",
            "\\": "\\",
            '"': '"',
            "'": "'",
        }

        while True:
            character = self._advance()
            if not character:
                raise self._error("unterminated string literal", line, column)
            if character == quote:
                return _Token("STRING", "".join(characters), line, column)
            if character in "\r\n":
                raise self._error("unescaped newline in string literal", line, column)
            if character != "\\":
                characters.append(character)
                continue

            escape_line, escape_column = self.line, self.column - 1
            escaped = self._advance()
            if not escaped:
                raise self._error(
                    "unterminated string escape", escape_line, escape_column
                )
            if escaped in escape_map:
                characters.append(escape_map[escaped])
                continue
            if escaped == "\n":
                characters.append("\n")
                continue
            if escaped == "\r":
                if self._peek() == "\n":
                    self._advance()
                characters.append("\n")
                continue
            if escaped.isdigit():
                digits = escaped
                while len(digits) < 3 and self._peek().isdigit():
                    digits += self._advance()
                codepoint = int(digits)
                if codepoint > 255:
                    raise self._error(
                        f"decimal string escape out of range: {digits}",
                        escape_line,
                        escape_column,
                    )
                characters.append(chr(codepoint))
                continue
            if escaped == "x":
                digits = self._advance() + self._advance()
                if len(digits) != 2 or any(
                    ch not in "0123456789abcdefABCDEF" for ch in digits
                ):
                    raise self._error(
                        "hex string escape requires two hexadecimal digits",
                        escape_line,
                        escape_column,
                    )
                characters.append(chr(int(digits, 16)))
                continue
            raise self._error(
                f"unsupported string escape \\{escaped}", escape_line, escape_column
            )

    def next_token(self) -> _Token:
        self._skip_space_and_comments()
        line, column = self.line, self.column
        character = self._peek()
        if not character:
            return _Token("EOF", None, line, column)
        if character in "{}[]=,;":
            self._advance()
            return _Token(character, character, line, column)
        if character in "\"'":
            return self._read_string()

        number_match = _NUMBER_LITERAL.match(self.text, self.index)
        if number_match is not None:
            literal = number_match.group(0)
            for _ in literal:
                self._advance()
            if any(marker in literal for marker in ".eE"):
                value: int | float = float(literal)
                if not math.isfinite(value):
                    raise self._error(
                        "number is outside the finite JSON range", line, column
                    )
            else:
                value = int(literal)
            return _Token("NUMBER", value, line, column)

        identifier_match = _IDENTIFIER.match(self.text, self.index)
        if identifier_match is not None:
            identifier = identifier_match.group(0)
            for _ in identifier:
                self._advance()
            return _Token("IDENT", identifier, line, column)

        raise self._error(f"unexpected character {character!r}")


class _Parser:
    def __init__(self, text: str, source_name: str) -> None:
        self.source_name = source_name
        self.lexer = _Lexer(text, source_name)
        self.current = self.lexer.next_token()
        self.lookahead = self.lexer.next_token()

    def _advance(self) -> _Token:
        token = self.current
        self.current = self.lookahead
        self.lookahead = self.lexer.next_token()
        return token

    def _error(
        self, message: str, token: _Token | None = None
    ) -> SavedVariablesSyntaxError:
        selected = token or self.current
        return SavedVariablesSyntaxError(
            f"{self.source_name}:{selected.line}:{selected.column}: {message}"
        )

    def _expect(self, kind: str) -> _Token:
        if self.current.kind != kind:
            raise self._error(f"expected {kind!r}, got {self.current.kind!r}")
        return self._advance()

    def parse(self) -> dict[str, Any]:
        assignments: dict[str, Any] = {}
        while self.current.kind != "EOF":
            name_token = self._expect("IDENT")
            self._expect("=")
            value = self._parse_value()
            if name_token.value in assignments:
                raise self._error(
                    f"duplicate top-level assignment {name_token.value!r}", name_token
                )
            assignments[name_token.value] = value
            if self.current.kind == ";":
                self._advance()
        return assignments

    def _parse_value(self) -> Any:
        if self.current.kind in {"STRING", "NUMBER"}:
            return self._advance().value
        if self.current.kind == "{":
            return self._parse_table()
        if self.current.kind == "IDENT":
            token = self._advance()
            if token.value == "true":
                return True
            if token.value == "false":
                return False
            if token.value == "nil":
                return None
            raise self._error(
                f"identifier value {token.value!r} is not a SavedVariables literal",
                token,
            )
        raise self._error(f"expected a literal value, got {self.current.kind!r}")

    def _parse_explicit_key(self) -> tuple[str | int, _Token]:
        self._expect("[")
        key_token = self.current
        if key_token.kind == "STRING":
            key: str | int = self._advance().value
        elif key_token.kind == "NUMBER" and type(key_token.value) is int:
            key = self._advance().value
        else:
            raise self._error("table key must be a string or integer", key_token)
        self._expect("]")
        self._expect("=")
        return key, key_token

    def _parse_table(self) -> _LuaTable:
        self._expect("{")
        result = _LuaTable()
        next_array_index = 1

        while self.current.kind != "}":
            if self.current.kind == "EOF":
                raise self._error("unterminated table constructor")

            if self.current.kind == "[":
                key, key_token = self._parse_explicit_key()
                value = self._parse_value()
            elif self.current.kind == "IDENT" and self.lookahead.kind == "=":
                key_token = self._advance()
                key = key_token.value
                self._expect("=")
                value = self._parse_value()
            else:
                key_token = self.current
                key = next_array_index
                next_array_index += 1
                value = self._parse_value()

            if key in result.fields:
                raise self._error(f"duplicate table key {key!r}", key_token)
            result.fields[key] = value

            if self.current.kind in {",", ";"}:
                self._advance()
            elif self.current.kind != "}":
                raise self._error("expected ',' or '}' after table field")

        self._expect("}")
        return result


def _utc_iso(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _schema_error(path: str, message: str) -> SavedVariablesSchemaError:
    return SavedVariablesSchemaError(f"{path}: {message}")


def _require_lua_table(value: Any, path: str) -> _LuaTable:
    if not isinstance(value, _LuaTable):
        raise _schema_error(path, f"expected table, got {type(value).__name__}")
    return value


def _require_string_keyed_table(value: Any, path: str) -> _LuaTable:
    table = _require_lua_table(value, path)
    invalid = [key for key in table.fields if not isinstance(key, str)]
    if invalid:
        raise _schema_error(path, f"expected named fields, found keys {invalid!r}")
    return table


def _array_items(value: Any, path: str) -> list[tuple[int, Any]]:
    table = _require_lua_table(value, path)
    keys = sorted(table.fields)
    if any(type(key) is not int or key < 1 for key in keys):
        raise _schema_error(path, "array keys must be positive integers")
    expected = list(range(1, len(keys) + 1))
    if keys != expected:
        raise _schema_error(path, f"array keys must be contiguous from 1, got {keys!r}")
    return [(key, table.fields[key]) for key in keys]


def _empty_table_is_array(path: str) -> bool:
    final_component = path.rsplit(".", 1)[-1]
    return final_component in _EMPTY_ARRAY_FIELDS


def _task_index_map_items(value: Any, path: str) -> list[tuple[int, Any]]:
    table = _require_lua_table(value, path)
    keys = sorted(table.fields)
    if any(type(key) is not int or key < 1 for key in keys):
        raise _schema_error(path, "task-index map keys must be positive integers")
    return [(key, table.fields[key]) for key in keys]


def _to_json_value(value: Any, path: str) -> Any:
    if value is None or isinstance(value, (str, bool)) or type(value) is int:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise _schema_error(path, "number must be finite")
        return value
    if not isinstance(value, _LuaTable):
        raise _schema_error(path, f"unsupported value type {type(value).__name__}")

    if not value.fields:
        return [] if _empty_table_is_array(path) else {}
    if all(type(key) is int for key in value.fields):
        final_component = path.rsplit(".", 1)[-1]
        if final_component in _TASK_INDEX_MAP_FIELDS:
            return {
                str(key): _to_json_value(item, f"{path}[{key}]")
                for key, item in _task_index_map_items(value, path)
            }
        return [
            _to_json_value(item, f"{path}[{key}]")
            for key, item in _array_items(value, path)
        ]
    if all(isinstance(key, str) for key in value.fields):
        return {
            key: _to_json_value(item, f"{path}.{key}")
            for key, item in value.fields.items()
        }
    raise _schema_error(path, "table mixes array and named fields")


def _require_exact_int(record: dict[str, Any], key: str, path: str) -> int:
    if key not in record:
        raise _schema_error(path, f"missing required field {key!r}")
    value = record[key]
    if type(value) is not int:
        raise _schema_error(
            f"{path}.{key}", f"expected integer, got {type(value).__name__}"
        )
    return value


def _require_number(record: dict[str, Any], key: str, path: str) -> int | float:
    if key not in record:
        raise _schema_error(path, f"missing required field {key!r}")
    value = record[key]
    if type(value) not in {int, float}:
        raise _schema_error(
            f"{path}.{key}", f"expected number, got {type(value).__name__}"
        )
    return value


def _require_nonempty_string(record: dict[str, Any], key: str, path: str) -> str:
    if key not in record:
        raise _schema_error(path, f"missing required field {key!r}")
    value = record[key]
    if not isinstance(value, str) or not value:
        raise _schema_error(f"{path}.{key}", "expected non-empty string")
    return value


def _require_type(record: dict[str, Any], key: str, expected: type, path: str) -> Any:
    if key not in record:
        raise _schema_error(path, f"missing required field {key!r}")
    value = record[key]
    if type(value) is not expected:
        raise _schema_error(
            f"{path}.{key}", f"expected {expected.__name__}, got {type(value).__name__}"
        )
    return value


def _validate_decision(record: dict[str, Any], path: str) -> None:
    if "provenance" in record:
        raise _schema_error(path, "reserved field 'provenance' is already present")
    version = _require_exact_int(record, "schemaVersion", path)
    if version not in {1, 2}:
        raise _schema_error(
            f"{path}.schemaVersion", "only schema versions 1 and 2 are supported"
        )
    _require_number(record, "capturedAt", path)
    mode = _require_nonempty_string(record, "mode", path)
    if mode not in {"shadow", "live"}:
        raise _schema_error(
            f"{path}.mode", f"expected 'shadow' or 'live', got {mode!r}"
        )
    _require_type(record, "state", dict, path)
    proposal = _require_type(record, "proposal", dict, path)
    required_stages = ("queue", "gcd") if version == 1 else ("off_gcd", "queue", "gcd")
    for stage in required_stages:
        actions = _require_type(proposal, stage, list, f"{path}.proposal")
        if any(not isinstance(action, str) or not action for action in actions):
            raise _schema_error(
                f"{path}.proposal.{stage}", "action IDs must be non-empty strings"
            )
    if version == 1 and "off_gcd" in proposal:
        actions = _require_type(proposal, "off_gcd", list, f"{path}.proposal")
        if any(not isinstance(action, str) or not action for action in actions):
            raise _schema_error(
                f"{path}.proposal.off_gcd", "action IDs must be non-empty strings"
            )
    attempts = _require_type(record, "attempts", list, path)
    if any(not isinstance(attempt, dict) for attempt in attempts):
        raise _schema_error(f"{path}.attempts", "every attempt must be an object")
    _require_type(record, "stopped", bool, path)

    if version == 1:
        return

    decision_id = _require_nonempty_string(record, "decisionId", path)
    _require_nonempty_string(record, "runtimeSessionId", path)

    def validate_proposal(value: Any, value_path: str) -> None:
        if not isinstance(value, dict):
            raise _schema_error(value_path, "expected object")
        for stage in ("off_gcd", "queue", "gcd"):
            lane = _require_type(value, stage, list, value_path)
            if any(not isinstance(action, str) or not action for action in lane):
                raise _schema_error(
                    f"{value_path}.{stage}", "action IDs must be non-empty strings"
                )

    def validate_attempts(value: Any, value_path: str) -> None:
        if not isinstance(value, list):
            raise _schema_error(value_path, "expected array")
        if any(not isinstance(attempt, dict) for attempt in value):
            raise _schema_error(value_path, "every attempt must be an object")

    def validate_sink_actions(value: Any, value_path: str) -> None:
        if not isinstance(value, list):
            raise _schema_error(value_path, "expected array")
        for expected_sequence, action in enumerate(value, start=1):
            action_path = f"{value_path}[{expected_sequence}]"
            if not isinstance(action, dict):
                raise _schema_error(action_path, "action must be an object")
            sequence = _require_exact_int(action, "seq", action_path)
            if sequence != expected_sequence:
                raise _schema_error(
                    f"{action_path}.seq",
                    f"expected {expected_sequence}, got {sequence}",
                )
            _require_nonempty_string(action, "api", action_path)
            _require_nonempty_string(action, "channel", action_path)
            _require_type(action, "args", dict, action_path)
            action_decision_id = _require_nonempty_string(
                action, "decisionId", action_path
            )
            if action_decision_id != decision_id:
                raise _schema_error(
                    f"{action_path}.decisionId",
                    f"expected {decision_id!r}, got {action_decision_id!r}",
                )

    active = _require_type(record, "activePolicy", dict, path)
    _require_nonempty_string(active, "requestedPolicyId", f"{path}.activePolicy")
    _require_type(active, "proposalAvailable", bool, f"{path}.activePolicy")
    validate_proposal(active.get("proposal"), f"{path}.activePolicy.proposal")
    _require_type(active, "executionRequested", bool, f"{path}.activePolicy")
    validate_attempts(active.get("attempts"), f"{path}.activePolicy.attempts")
    _require_type(active, "stopped", bool, f"{path}.activePolicy")

    candidate = _require_type(record, "candidateShadow", dict, path)
    candidate_policy_id = _require_nonempty_string(
        candidate, "requestedPolicyId", f"{path}.candidateShadow"
    )
    if candidate_policy_id != "fury_combined_candidate_shadow_v1":
        raise _schema_error(
            f"{path}.candidateShadow.requestedPolicyId",
            "expected 'fury_combined_candidate_shadow_v1'",
        )
    _require_type(candidate, "proposalAvailable", bool, f"{path}.candidateShadow")
    validate_proposal(candidate.get("proposal"), f"{path}.candidateShadow.proposal")
    candidate_executed = _require_type(
        candidate, "executed", bool, f"{path}.candidateShadow"
    )
    if candidate_executed:
        raise _schema_error(
            f"{path}.candidateShadow.executed",
            "inactive shadow candidate must not be executed",
        )

    actual = _require_type(record, "expertActual", dict, path)
    _require_type(actual, "observed", bool, f"{path}.expertActual")
    _require_nonempty_string(actual, "sourceKind", f"{path}.expertActual")
    _require_type(actual, "proposalAvailable", bool, f"{path}.expertActual")
    validate_proposal(actual.get("proposal"), f"{path}.expertActual.proposal")
    _require_type(actual, "executed", bool, f"{path}.expertActual")
    actual_materialized = actual.get("materialized")
    if actual_materialized is not None and not isinstance(actual_materialized, bool):
        raise _schema_error(
            f"{path}.expertActual.materialized", "expected boolean"
        )
    validate_sink_actions(actual.get("sinkActions"), f"{path}.expertActual.sinkActions")
    server_observed_actions = _require_type(
        actual, "serverObservedActions", list, f"{path}.expertActual"
    )
    for action_index, action in enumerate(server_observed_actions, start=1):
        action_path = f"{path}.expertActual.serverObservedActions[{action_index}]"
        if not isinstance(action, dict):
            raise _schema_error(action_path, "action must be an object")
        sequence = _require_exact_int(action, "seq", action_path)
        if sequence != action_index:
            raise _schema_error(
                f"{action_path}.seq", f"expected {action_index}, got {sequence}"
            )
        action_decision_id = _require_nonempty_string(action, "decisionId", action_path)
        if action_decision_id != decision_id:
            raise _schema_error(
                f"{action_path}.decisionId",
                f"expected {decision_id!r}, got {action_decision_id!r}",
            )
        _require_number(action, "time", action_path)
        _require_nonempty_string(action, "event", action_path)
        spell_id = _require_exact_int(action, "spellID", action_path)
        if spell_id < 1:
            raise _schema_error(f"{action_path}.spellID", "must be positive")
        _require_nonempty_string(action, "family", action_path)
        _require_nonempty_string(action, "attribution", action_path)
    links = _require_type(actual, "expertTraceLinks", list, f"{path}.expertActual")
    if any(not isinstance(link, dict) for link in links):
        raise _schema_error(
            f"{path}.expertActual.expertTraceLinks",
            "every trace link must be an object",
        )

    outcome = _require_type(record, "observedActualOutcome", dict, path)
    observed_only = _require_type(
        outcome, "observedOnly", bool, f"{path}.observedActualOutcome"
    )
    if not observed_only:
        raise _schema_error(
            f"{path}.observedActualOutcome.observedOnly", "expected true"
        )
    attribution = _require_nonempty_string(
        outcome, "attribution", f"{path}.observedActualOutcome"
    )
    if attribution != "expert_actual_only_not_candidate_counterfactual":
        raise _schema_error(
            f"{path}.observedActualOutcome.attribution",
            "must identify actual-only observational evidence",
        )
    _require_nonempty_string(outcome, "status", f"{path}.observedActualOutcome")
    _require_number(outcome, "windowSeconds", f"{path}.observedActualOutcome")
    outcome_actions = _require_type(
        outcome, "actions", list, f"{path}.observedActualOutcome"
    )
    for action_index, action in enumerate(outcome_actions, start=1):
        action_path = f"{path}.observedActualOutcome.actions[{action_index}]"
        if not isinstance(action, dict):
            raise _schema_error(action_path, "action must be an object")
        _require_nonempty_string(action, "family", action_path)
        _require_nonempty_string(action, "status", action_path)
        spell_ids = _require_type(action, "expectedSpellIds", list, action_path)
        if any(type(spell_id) is not int or spell_id < 1 for spell_id in spell_ids):
            raise _schema_error(
                f"{action_path}.expectedSpellIds",
                "spell IDs must be positive integers",
            )
    event_sequences = _require_type(
        outcome, "eventSequences", list, f"{path}.observedActualOutcome"
    )
    if any(type(sequence) is not int or sequence < 1 for sequence in event_sequences):
        raise _schema_error(
            f"{path}.observedActualOutcome.eventSequences",
            "event sequences must be positive integers",
        )
    compact_events = _require_type(
        outcome, "compactEvents", list, f"{path}.observedActualOutcome"
    )
    for event_index, compact_event in enumerate(compact_events, start=1):
        event_path = f"{path}.observedActualOutcome.compactEvents[{event_index}]"
        if not isinstance(compact_event, dict):
            raise _schema_error(event_path, "event must be an object")
        _require_number(compact_event, "time", event_path)
        _require_nonempty_string(compact_event, "event", event_path)
        _require_nonempty_string(compact_event, "telemetrySource", event_path)
        if "spellID" in compact_event:
            spell_id = _require_exact_int(compact_event, "spellID", event_path)
            if spell_id < 1:
                raise _schema_error(f"{event_path}.spellID", "must be positive")
        if "amount" in compact_event:
            _require_number(compact_event, "amount", event_path)
    telemetry = _require_type(
        outcome, "telemetry", dict, f"{path}.observedActualOutcome"
    )
    _require_type(
        telemetry, "available", bool, f"{path}.observedActualOutcome.telemetry"
    )
    _require_nonempty_string(
        telemetry, "source", f"{path}.observedActualOutcome.telemetry"
    )
    _require_nonempty_string(
        telemetry, "reason", f"{path}.observedActualOutcome.telemetry"
    )
    registered_event_count = _require_exact_int(
        telemetry,
        "registeredEventCount",
        f"{path}.observedActualOutcome.telemetry",
    )
    if registered_event_count < 0:
        raise _schema_error(
            f"{path}.observedActualOutcome.telemetry.registeredEventCount",
            "must not be negative",
        )
    missing_cvars = _require_type(
        telemetry,
        "missingCVars",
        list,
        f"{path}.observedActualOutcome.telemetry",
    )
    if any(not isinstance(cvar, str) or not cvar for cvar in missing_cvars):
        raise _schema_error(
            f"{path}.observedActualOutcome.telemetry.missingCVars",
            "CVar names must be non-empty strings",
        )

    # Pre-contract schema-v2 rows remain readable so the watcher can ignore a
    # SavedVariables write produced just before the fixed addon loads. New
    # rows carry this strict event-confirmed sample block.
    sample = record.get("shadowSample")
    if sample is None:
        return
    if not isinstance(sample, dict):
        raise _schema_error(f"{path}.shadowSample", "expected object")
    contract_version = _require_exact_int(
        sample, "contractVersion", f"{path}.shadowSample"
    )
    if contract_version not in SUPPORTED_SHADOW_CONTRACT_VERSIONS:
        supported = ", ".join(
            str(version) for version in sorted(SUPPORTED_SHADOW_CONTRACT_VERSIONS)
        )
        raise _schema_error(
            f"{path}.shadowSample.contractVersion",
            f"expected one of {supported}",
        )
    status = _require_nonempty_string(sample, "status", f"{path}.shadowSample")
    if status not in {"pending", "bound", "confirmed", "failed", "expired", "ineligible"}:
        raise _schema_error(f"{path}.shadowSample.status", "unknown status")
    materialized = _require_type(
        sample, "materialized", bool, f"{path}.shadowSample"
    )
    counted = _require_type(sample, "counted", bool, f"{path}.shadowSample")
    coalesced = _require_exact_int(
        sample, "coalescedMacroEvaluations", f"{path}.shadowSample"
    )
    if coalesced < 1:
        raise _schema_error(
            f"{path}.shadowSample.coalescedMacroEvaluations", "must be positive"
        )
    _require_number(sample, "armedAt", f"{path}.shadowSample")
    if status == "confirmed":
        if not materialized:
            raise _schema_error(
                f"{path}.shadowSample.materialized",
                "confirmed sample must be materialized",
            )
        _require_number(sample, "confirmedAt", f"{path}.shadowSample")
        _require_nonempty_string(
            sample, "confirmedEvent", f"{path}.shadowSample"
        )
        _require_nonempty_string(
            sample, "actionFamily", f"{path}.shadowSample"
        )
        _require_nonempty_string(sample, "outcomeKind", f"{path}.shadowSample")
        if actual_materialized is not True:
            raise _schema_error(
                f"{path}.expertActual.materialized",
                "confirmed sample requires materialized actual action",
            )
        if counted:
            _require_number(sample, "countedAt", f"{path}.shadowSample")
    elif materialized or counted:
        raise _schema_error(
            f"{path}.shadowSample",
            "only confirmed samples may be materialized or counted",
        )
    if "exportSessionId" in sample:
        _require_nonempty_string(
            sample, "exportSessionId", f"{path}.shadowSample"
        )
    if "exportTransport" in sample:
        _require_nonempty_string(
            sample, "exportTransport", f"{path}.shadowSample"
        )
    if "exportedAt" in sample:
        _require_number(sample, "exportedAt", f"{path}.shadowSample")


def _validate_calibration(record: dict[str, Any], path: str) -> int:
    if "provenance" in record:
        raise _schema_error(path, "reserved field 'provenance' is already present")
    sequence = _require_exact_int(record, "sequence", path)
    if sequence < 1:
        raise _schema_error(f"{path}.sequence", "must be positive")
    _require_number(record, "time", path)
    _require_nonempty_string(record, "event", path)
    _require_type(record, "state", dict, path)
    if "intervalDecisionId" in record:
        _require_nonempty_string(record, "intervalDecisionId", path)
    if "actionDecisionId" in record:
        _require_nonempty_string(record, "actionDecisionId", path)
    return sequence


def _validate_expert_trace(record: dict[str, Any], path: str) -> int:
    if "provenance" in record:
        raise _schema_error(path, "reserved field 'provenance' is already present")
    sequence = _require_exact_int(record, "sequence", path)
    if sequence < 1:
        raise _schema_error(f"{path}.sequence", "must be positive")
    _require_number(record, "time", path)
    _require_nonempty_string(record, "expert", path)
    _require_nonempty_string(record, "entry", path)
    _require_type(record, "request", dict, path)
    _require_type(record, "state", dict, path)
    decision_id = None
    if "decisionId" in record:
        decision_id = _require_nonempty_string(record, "decisionId", path)
    actions = _require_type(record, "actions", list, path)
    expected_action_sequence = 1
    for action_index, action in enumerate(actions, start=1):
        action_path = f"{path}.actions[{action_index}]"
        if not isinstance(action, dict):
            raise _schema_error(action_path, "action must be an object")
        action_sequence = _require_exact_int(action, "seq", action_path)
        if action_sequence != expected_action_sequence:
            raise _schema_error(
                f"{action_path}.seq",
                f"expected {expected_action_sequence}, got {action_sequence}",
            )
        expected_action_sequence += 1
        _require_nonempty_string(action, "api", action_path)
        _require_nonempty_string(action, "channel", action_path)
        _require_type(action, "args", dict, action_path)
        if "decisionId" in action:
            action_decision_id = _require_nonempty_string(
                action, "decisionId", action_path
            )
            if decision_id is not None and action_decision_id != decision_id:
                raise _schema_error(
                    f"{action_path}.decisionId",
                    f"expected {decision_id!r}, got {action_decision_id!r}",
                )
    completed = record.get("completed") is True
    errored = record.get("errored") is True
    if completed == errored:
        raise _schema_error(path, "exactly one of completed or errored must be true")
    if "policyFamily" in record:
        _require_nonempty_string(record, "policyFamily", path)
    if "policyEntered" in record:
        _require_type(record, "policyEntered", bool, path)
    if "policyFunction" in record:
        _require_nonempty_string(record, "policyFunction", path)
        if record.get("policyEntered") is not True:
            raise _schema_error(
                f"{path}.policyFunction",
                "requires policyEntered=true",
            )
    if record.get("policyEntered") is True and "policyFunction" not in record:
        raise _schema_error(path, "policyEntered=true requires policyFunction")
    return sequence


def _records_from_array(value: Any, path: str) -> list[tuple[int, dict[str, Any]]]:
    records: list[tuple[int, dict[str, Any]]] = []
    for storage_index, raw_record in _array_items(value, path):
        record_path = f"{path}[{storage_index}]"
        converted = _to_json_value(raw_record, record_path)
        if not isinstance(converted, dict):
            raise _schema_error(record_path, "entry must be a table with named fields")
        records.append((storage_index, converted))
    return records


def _extract_records(
    assignments: dict[str, Any],
) -> tuple[
    list[tuple[int, dict[str, Any]]],
    list[tuple[int, dict[str, Any]]],
    list[tuple[int, dict[str, Any]]],
]:
    root_path = "BrainOfCatCharacterDB"
    if root_path not in assignments:
        raise _schema_error(root_path, "top-level assignment is missing")
    root = _require_string_keyed_table(assignments[root_path], root_path)
    root_version = root.fields.get("schemaVersion")
    if type(root_version) is not int or root_version != 1:
        raise _schema_error(f"{root_path}.schemaVersion", "expected integer value 1")

    decisions_path = f"{root_path}.entries"
    if "entries" in root.fields:
        decisions = _records_from_array(root.fields["entries"], decisions_path)
    else:
        decisions = []
    for storage_index, record in decisions:
        _validate_decision(record, f"{decisions_path}[{storage_index}]")

    def extract_ring(
        field_name: str,
        validator: Any,
    ) -> list[tuple[int, dict[str, Any]]]:
        ring_path = f"{root_path}.{field_name}"
        if field_name not in root.fields:
            return []
        ring_root = _require_string_keyed_table(root.fields[field_name], ring_path)
        ring_version = ring_root.fields.get("schemaVersion")
        if type(ring_version) is not int or ring_version != 1:
            raise _schema_error(
                f"{ring_path}.schemaVersion", "expected integer value 1"
            )
        if "entries" not in ring_root.fields:
            raise _schema_error(ring_path, "missing required field 'entries'")

        entries_path = f"{ring_path}.entries"
        records = _records_from_array(ring_root.fields["entries"], entries_path)
        sequences: dict[int, int] = {}
        sortable: list[tuple[int, int, dict[str, Any]]] = []
        for storage_index, record in records:
            record_path = f"{entries_path}[{storage_index}]"
            sequence = validator(record, record_path)
            if sequence in sequences:
                raise _schema_error(
                    record_path,
                    f"duplicate sequence {sequence}; first seen at storage index {sequences[sequence]}",
                )
            sequences[sequence] = storage_index
            sortable.append((sequence, storage_index, record))

        if "count" in ring_root.fields:
            count = ring_root.fields["count"]
            if type(count) is not int:
                raise _schema_error(f"{ring_path}.count", "expected integer")
            if count != len(records):
                raise _schema_error(
                    f"{ring_path}.count",
                    f"declares {count}, but entries contains {len(records)} records",
                )
        sortable.sort(key=lambda item: item[0])
        return [(storage_index, record) for _, storage_index, record in sortable]

    calibration = extract_ring("calibration", _validate_calibration)
    expert_traces = extract_ring("expertTraces", _validate_expert_trace)
    return decisions, calibration, expert_traces


def _decorate_records(
    records: list[tuple[int, dict[str, Any]]],
    *,
    kind: str,
    lua_path: str,
    source_name: str,
    raw_relative_path: str,
    imported_at: str,
) -> list[dict[str, Any]]:
    decorated: list[dict[str, Any]] = []
    for storage_index, record in records:
        output = dict(record)
        output["provenance"] = {
            "format": SOURCE_FORMAT,
            "kind": kind,
            "source_file": source_name,
            "raw_file": raw_relative_path,
            "lua_path": lua_path,
            "storage_index": storage_index,
            "imported_at": imported_at,
        }
        decorated.append(output)
    return decorated


def _safe_stem(source: Path) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", source.stem).strip("._")
    if not safe:
        raise SavedVariablesImportError("input filename has no usable stem")
    return safe


def _write_jsonl_temporary(
    directory: Path, prefix: str, records: list[dict[str, Any]]
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        prefix=f".{prefix}__",
        suffix=".jsonl.tmp",
        dir=directory,
        delete=False,
    ) as handle:
        for record in records:
            json.dump(
                record,
                handle,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
            handle.write("\n")
        return Path(handle.name)


def publish_shadow_pairs(
    decisions: Sequence[tuple[int, dict[str, Any]]],
    *,
    source_lua: str | Path,
    data_root: str | Path = DEFAULT_DATA_ROOT,
    source_signature: dict[str, int] | None = None,
) -> ShadowPairImportResult:
    """Append unseen counted, event-confirmed schema-v2 pairs without raw copies.

    ``decisions`` must come from :func:`_extract_records`, which has already
    applied the full schema-v1/v2 validator.  The normalized absolute source
    path plus intrinsic ``decisionId`` form the journal identity, because each
    character owns its local decision sequence.  No digest or raw-file copy is
    created.
    """

    source = Path(source_lua).expanduser().resolve()
    source_identity = os.path.normcase(os.path.normpath(str(source)))
    resolved_data_root = Path(data_root).expanduser().resolve()
    output_directory = resolved_data_root / "online_decisions"
    output = output_directory / "brainofcat_shadow_pairs_v2.jsonl"
    manifest = output_directory / "brainofcat_shadow_pairs_v2.manifest.json"

    source_pairs: list[tuple[int, dict[str, Any], str]] = []
    source_ids: set[tuple[str, str]] = set()
    for storage_index, record in decisions:
        if record.get("schemaVersion") != 2:
            continue
        candidate = record.get("candidateShadow")
        sample = record.get("shadowSample")
        if (
            not isinstance(candidate, dict)
            or candidate.get("requestedPolicyId")
            != "fury_combined_candidate_shadow_v1"
            or candidate.get("policyId")
            != "fury_combined_candidate_shadow_v1"
            or candidate.get("proposalAvailable") is not True
            or candidate.get("executed") is not False
            or not isinstance(sample, dict)
            or sample.get("contractVersion")
            not in SUPPORTED_SHADOW_CONTRACT_VERSIONS
            or sample.get("status") != "confirmed"
            or sample.get("materialized") is not True
            or sample.get("counted") is not True
        ):
            continue
        decision_id = record["decisionId"]
        export_session_id = sample.get("exportSessionId")
        pair_source_identity = (
            f"brainofcat-shadow-session:{export_session_id}"
            if isinstance(export_session_id, str) and export_session_id
            else source_identity
        )
        pair_identity = (pair_source_identity, decision_id)
        if pair_identity in source_ids:
            raise SavedVariablesSchemaError(
                "BrainOfCatCharacterDB.entries contains duplicate schema-v2 "
                f"Shadow pair identity {pair_identity!r}"
            )
        source_ids.add(pair_identity)
        source_pairs.append((storage_index, record, pair_source_identity))

    journal_identities: list[tuple[str, str]] = []
    journal_identity_set: set[tuple[str, str]] = set()
    if output.is_file():
        try:
            with output.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    decision_id = row.get("decisionId") if isinstance(row, dict) else None
                    if not isinstance(decision_id, str) or not decision_id:
                        raise ValueError(
                            f"missing decisionId at {output}:{line_number}"
                        )
                    provenance = row.get("provenance")
                    row_source = (
                        provenance.get("source_identity")
                        if isinstance(provenance, dict)
                        else None
                    )
                    if not isinstance(row_source, str) or not row_source:
                        row_source_absolute = (
                            provenance.get("source_absolute_path")
                            if isinstance(provenance, dict)
                            else None
                        )
                        if not isinstance(row_source_absolute, str) or not row_source_absolute:
                            raise ValueError(
                                f"missing source identity at {output}:{line_number}"
                            )
                        row_source = os.path.normcase(
                            os.path.normpath(row_source_absolute)
                        )
                    identity = (row_source, decision_id)
                    if identity in journal_identity_set:
                        raise ValueError(
                            f"duplicate source/decisionId identity {identity!r} at "
                            f"{output}:{line_number}"
                        )
                    journal_identities.append(identity)
                    journal_identity_set.add(identity)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise SavedVariablesImportError(
                f"cannot read compact Shadow pair journal {output}: {error}"
            ) from error

    imported_at = _utc_iso(datetime.now(timezone.utc))
    new_pairs = [
        (storage_index, record, pair_source_identity)
        for storage_index, record, pair_source_identity in source_pairs
        if (pair_source_identity, record["decisionId"])
        not in journal_identity_set
    ]
    output_directory.mkdir(parents=True, exist_ok=True)
    try:
        with output.open("a", encoding="utf-8", newline="\n") as handle:
            for storage_index, record, pair_source_identity in new_pairs:
                row = dict(record)
                row["provenance"] = {
                    "format": SOURCE_FORMAT,
                    "kind": "shadow_decision_pair",
                    "source_file": source.name,
                    "source_absolute_path": str(source),
                    "source_identity": pair_source_identity,
                    "source_physical_identity": source_identity,
                    "lua_path": "BrainOfCatCharacterDB.entries",
                    "storage_index": storage_index,
                    "source_signature": dict(source_signature or {}),
                    "imported_at": imported_at,
                }
                json.dump(
                    row,
                    handle,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
                handle.write("\n")
                identity = (pair_source_identity, record["decisionId"])
                journal_identities.append(identity)
                journal_identity_set.add(identity)

        manifest_document = {
            "schema_version": 2,
            "kind": "brainofcat_shadow_pair_journal",
            "source": str(source),
            "output": str(output),
            "journal_pair_total": len(journal_identities),
            "identities": [
                {"source": identity_source, "decision_id": decision_id}
                for identity_source, decision_id in journal_identities
            ],
            "last_observed": {
                "observed_at": imported_at,
                "source_signature": dict(source_signature or {}),
                "source_pair_total": len(source_pairs),
                "new_pair_count": len(new_pairs),
            },
        }
        temporary = manifest.with_name(manifest.name + ".tmp")
        temporary.write_text(
            json.dumps(
                manifest_document,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(manifest)
    except (OSError, TypeError, ValueError) as error:
        raise SavedVariablesImportError(
            f"failed to publish compact Shadow pairs from {source}: {error}"
        ) from error

    return ShadowPairImportResult(
        source_pair_total=len(source_pairs),
        new_pair_count=len(new_pairs),
        journal_pair_total=len(journal_identities),
        output=output,
        manifest=manifest,
    )


def import_savedvariables(
    source_lua: str | Path,
    *,
    data_root: str | Path = DEFAULT_DATA_ROOT,
) -> SavedVariablesImportResult:
    """Import the one BrainOfCat.lua path supplied by the caller."""

    source = Path(source_lua).expanduser().resolve()
    if not source.is_file():
        raise SavedVariablesImportError(
            f"input SavedVariables file does not exist or is not a file: {source}"
        )

    try:
        text = source.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as error:
        raise SavedVariablesImportError(f"failed to read {source}: {error}") from error

    assignments = _Parser(text, source.name).parse()
    decisions, calibration, expert_traces = _extract_records(assignments)

    resolved_data_root = Path(data_root).expanduser().resolve()
    now = datetime.now(timezone.utc)
    imported_at = _utc_iso(now)
    import_id = now.strftime("%Y%m%dT%H%M%S%fZ")
    safe_source = _safe_stem(source)

    raw_dir = resolved_data_root / "online_raw" / import_id
    raw_copy = raw_dir / source.name
    metadata_path = raw_dir / "provenance.json"
    decision_dir = resolved_data_root / "online_decisions"
    calibration_dir = resolved_data_root / "calibration"
    expert_trace_dir = resolved_data_root / "expert_traces"
    decision_path = decision_dir / f"{safe_source}__{import_id}.jsonl"
    calibration_path = calibration_dir / f"{safe_source}__{import_id}.jsonl"
    expert_trace_path = expert_trace_dir / f"{safe_source}__{import_id}.jsonl"
    raw_relative_path = raw_copy.relative_to(resolved_data_root).as_posix()

    decision_rows = _decorate_records(
        decisions,
        kind="online_decision",
        lua_path="BrainOfCatCharacterDB.entries",
        source_name=source.name,
        raw_relative_path=raw_relative_path,
        imported_at=imported_at,
    )
    calibration_rows = _decorate_records(
        calibration,
        kind="calibration",
        lua_path="BrainOfCatCharacterDB.calibration.entries",
        source_name=source.name,
        raw_relative_path=raw_relative_path,
        imported_at=imported_at,
    )
    expert_trace_rows = _decorate_records(
        expert_traces,
        kind="expert_request_trace",
        lua_path="BrainOfCatCharacterDB.expertTraces.entries",
        source_name=source.name,
        raw_relative_path=raw_relative_path,
        imported_at=imported_at,
    )

    decision_temporary: Path | None = None
    calibration_temporary: Path | None = None
    expert_trace_temporary: Path | None = None
    try:
        decision_temporary = _write_jsonl_temporary(
            decision_dir, safe_source, decision_rows
        )
        calibration_temporary = _write_jsonl_temporary(
            calibration_dir, safe_source, calibration_rows
        )
        expert_trace_temporary = _write_jsonl_temporary(
            expert_trace_dir, safe_source, expert_trace_rows
        )

        raw_dir.mkdir(parents=True, exist_ok=False)
        shutil.copy2(source, raw_copy)

        source_stat = source.stat()
        metadata = {
            "schema_version": SCHEMA_VERSION,
            "source_format": SOURCE_FORMAT,
            "source": {
                "absolute_path": str(source),
                "filename": source.name,
                "size_bytes": source_stat.st_size,
                "modified_at": _utc_iso(
                    datetime.fromtimestamp(source_stat.st_mtime, tz=timezone.utc)
                ),
            },
            "imported_at": imported_at,
            "decision_count": len(decision_rows),
            "calibration_count": len(calibration_rows),
            "expert_trace_count": len(expert_trace_rows),
            "raw_copy": raw_relative_path,
            "online_decisions": decision_path.relative_to(
                resolved_data_root
            ).as_posix(),
            "calibration": calibration_path.relative_to(resolved_data_root).as_posix(),
            "expert_traces": expert_trace_path.relative_to(
                resolved_data_root
            ).as_posix(),
            "acquisition": "explicit user-selected WoW SavedVariables file",
        }
        with metadata_path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(metadata, handle, ensure_ascii=False, indent=2)
            handle.write("\n")

        decision_temporary.replace(decision_path)
        decision_temporary = None
        calibration_temporary.replace(calibration_path)
        calibration_temporary = None
        expert_trace_temporary.replace(expert_trace_path)
        expert_trace_temporary = None
    except (OSError, TypeError, ValueError) as error:
        raise SavedVariablesImportError(
            f"failed to publish import for {source}: {error}"
        ) from error
    finally:
        if decision_temporary is not None:
            decision_temporary.unlink(missing_ok=True)
        if calibration_temporary is not None:
            calibration_temporary.unlink(missing_ok=True)
        if expert_trace_temporary is not None:
            expert_trace_temporary.unlink(missing_ok=True)

    return SavedVariablesImportResult(
        decision_count=len(decision_rows),
        calibration_count=len(calibration_rows),
        expert_trace_count=len(expert_trace_rows),
        raw_copy=raw_copy,
        online_decisions=decision_path,
        calibration=calibration_path,
        expert_traces=expert_trace_path,
        metadata=metadata_path,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="import_savedvariables",
        description=(
            "Import one explicitly selected BrainOfCat.lua file. "
            "The command does not scan for SavedVariables."
        ),
        epilog=(
            "Windows example:\n"
            "  py -m o2o_dps.import_savedvariables "
            '"D:\\World of Warcraft\\WTF\\Account\\ACCOUNT\\Realm\\Character\\'
            'SavedVariables\\BrainOfCat.lua"'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("lua", type=Path, help="explicit path to BrainOfCat.lua")
    parser.add_argument(
        "--data-root",
        type=Path,
        default=DEFAULT_DATA_ROOT,
        help=f"offline data root (default: {DEFAULT_DATA_ROOT})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        result = import_savedvariables(args.lua, data_root=args.data_root)
    except SavedVariablesImportError as error:
        print(f"SavedVariables import failed: {error}", file=sys.stderr)
        return 2

    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
