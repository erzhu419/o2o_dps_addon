"""Import Nampower's append-only, event-confirmed Shadow pair export.

The addon writes one small JSON envelope per confirmed action to
``WoW/CustomData/BrainOfCatShadowPairs.jsonl``.  This path is independent of
WoW SavedVariables, whose oversized historical BrainOfCat file may fail to
rotate.  The collector never copies the CustomData file into ``online_raw``;
it publishes only validated schema-v2 pairs through the compact journal.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Callable

from .import_savedvariables import (
    SavedVariablesImportError,
    SavedVariablesSchemaError,
    _validate_decision,
    publish_shadow_pairs,
)
from .timer_live_debug import derive_wow_root


EXPORT_SCHEMA = "brainofcat_shadow_pair_export/v1"
EXPORT_SCHEMA_VERSION = 1
EXPORT_FILE_NAME = "BrainOfCatShadowPairs.jsonl"


class ShadowLiveExportError(RuntimeError):
    """The current CustomData export cannot be read or validated."""


@dataclass(frozen=True)
class StableText:
    path: Path
    text: str
    size_bytes: int
    mtime_ns: int

    def signature(self) -> tuple[int, int]:
        return self.mtime_ns, self.size_bytes

    def signature_dict(self) -> dict[str, int]:
        return {"mtime_ns": self.mtime_ns, "size_bytes": self.size_bytes}


def _read_stable_text(path: Path) -> StableText:
    try:
        before = path.stat()
        text = path.read_text(encoding="utf-8")
        after = path.stat()
    except (OSError, UnicodeError) as error:
        raise ShadowLiveExportError(
            f"cannot read Shadow CustomData export {path}: {error}"
        ) from error
    before_signature = (before.st_mtime_ns, before.st_size)
    after_signature = (after.st_mtime_ns, after.st_size)
    if before_signature != after_signature:
        raise ShadowLiveExportError(
            f"Shadow CustomData export changed while being read: {path}"
        )
    return StableText(path, text, after.st_size, after.st_mtime_ns)


def _require_nonempty_text(document: dict[str, Any], key: str, path: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ShadowLiveExportError(f"{path}.{key} must be a non-empty string")
    return value


def _decode_export(
    stable: StableText,
) -> tuple[list[tuple[int, dict[str, Any]]], dict[str, Any]]:
    decisions: list[tuple[int, dict[str, Any]]] = []
    identities: set[tuple[str, str]] = set()
    session_ids: set[str] = set()
    session_diagnostics: dict[str, dict[str, Any]] = {}
    latest_export_session_id: str | None = None
    session_start_count = 0
    invalid_lines: list[dict[str, Any]] = []

    for line_number, line in enumerate(stable.text.splitlines(), start=1):
        if not line.strip():
            continue
        source_path = f"{stable.path}:{line_number}"
        try:
            envelope = json.loads(line)
            if not isinstance(envelope, dict):
                raise ShadowLiveExportError(f"{source_path} must contain a JSON object")
            if envelope.get("schema") != EXPORT_SCHEMA:
                raise ShadowLiveExportError(f"{source_path} has an unsupported schema")
            if envelope.get("schemaVersion") != EXPORT_SCHEMA_VERSION:
                raise ShadowLiveExportError(
                    f"{source_path}.schemaVersion must equal {EXPORT_SCHEMA_VERSION}"
                )
            kind = _require_nonempty_text(envelope, "kind", source_path)
            export_session_id = _require_nonempty_text(
                envelope, "exportSessionId", source_path
            )
            _require_nonempty_text(envelope, "characterKey", source_path)
            session_ids.add(export_session_id)
            if kind == "session_start":
                session_start_count += 1
                session_diagnostics.setdefault(
                    export_session_id,
                    {"ordinal": 0, "target": 0, "completed": False},
                )
                latest_export_session_id = export_session_id
                continue
            if kind != "shadow_pair":
                raise ShadowLiveExportError(
                    f"{source_path}.kind has unsupported value {kind!r}"
                )
            record = envelope.get("record")
            if not isinstance(record, dict):
                raise ShadowLiveExportError(f"{source_path}.record must be an object")
            try:
                _validate_decision(record, f"{source_path}.record")
            except SavedVariablesSchemaError as error:
                raise ShadowLiveExportError(str(error)) from error
            sample = record.get("shadowSample")
            candidate = record.get("candidateShadow")
            if (
                not isinstance(sample, dict)
                or sample.get("status") != "confirmed"
                or sample.get("materialized") is not True
                or sample.get("counted") is not True
                or sample.get("exportSessionId") != export_session_id
                or not isinstance(candidate, dict)
                or candidate.get("executed") is not False
            ):
                raise ShadowLiveExportError(
                    f"{source_path}.record is not an event-confirmed counted Shadow pair"
                )
            decision_id = record.get("decisionId")
            if not isinstance(decision_id, str) or not decision_id:
                raise ShadowLiveExportError(
                    f"{source_path}.record.decisionId must be a non-empty string"
                )
            identity = (export_session_id, decision_id)
            if identity in identities:
                raise ShadowLiveExportError(
                    f"{source_path} duplicates export session/decision identity {identity!r}"
                )
            identities.add(identity)
            decisions.append((line_number, record))
            ordinal = envelope.get("ordinal")
            target = envelope.get("target")
            latest_export_session_id = export_session_id
            current_session = session_diagnostics.setdefault(
                export_session_id,
                {"ordinal": 0, "target": 0, "completed": False},
            )
            if isinstance(ordinal, int) and not isinstance(ordinal, bool):
                current_session["ordinal"] = max(current_session["ordinal"], ordinal)
            if isinstance(target, int) and not isinstance(target, bool):
                current_session["target"] = max(current_session["target"], target)
            current_session["completed"] = (
                current_session["completed"] or envelope.get("completed") is True
            )
        except (json.JSONDecodeError, ShadowLiveExportError) as error:
            invalid_lines.append({"line": line_number, "error": str(error)})

    latest_session = session_diagnostics.get(latest_export_session_id or "", {})
    return decisions, {
        "session_count": len(session_ids),
        "session_start_count": session_start_count,
        "valid_pair_count": len(decisions),
        "invalid_line_count": len(invalid_lines),
        "invalid_lines": invalid_lines[-8:],
        "latest_export_session_id": latest_export_session_id,
        "latest_ordinal": int(latest_session.get("ordinal", 0) or 0),
        "latest_target": int(latest_session.get("target", 0) or 0),
        "completed": latest_session.get("completed") is True,
    }


class ShadowLiveExportCollector:
    """Poll one fixed CustomData JSONL and publish only newly seen pairs."""

    def __init__(
        self,
        savedvariables_path: str | Path,
        *,
        data_root: str | Path,
        publisher: Callable[..., Any] = publish_shadow_pairs,
    ) -> None:
        self.savedvariables_path = Path(savedvariables_path).expanduser().resolve()
        self.data_root = Path(data_root).expanduser().resolve()
        self.wow_root = derive_wow_root(self.savedvariables_path)
        self.publisher = publisher
        self._handled_signature: tuple[int, int] | None = None
        self._last_result: dict[str, Any] | None = None

    @property
    def export_path(self) -> Path | None:
        if self.wow_root is None:
            return None
        return self.wow_root / "CustomData" / EXPORT_FILE_NAME

    def poll(self) -> dict[str, Any]:
        export_path = self.export_path
        if export_path is None:
            return {
                "status": "not_available",
                "message": "The SavedVariables path has no WTF ancestor.",
                "changed": False,
            }
        if not export_path.is_file():
            return {
                "status": "waiting_for_export",
                "message": f"Waiting for {export_path}.",
                "source": str(export_path),
                "changed": False,
            }
        stable = _read_stable_text(export_path)
        if (
            stable.signature() == self._handled_signature
            and self._last_result is not None
        ):
            return {**self._last_result, "changed": False, "new_pair_count": 0}

        decisions, diagnostics = _decode_export(stable)
        try:
            published = self.publisher(
                decisions,
                source_lua=export_path,
                data_root=self.data_root,
                source_signature=stable.signature_dict(),
            )
        except SavedVariablesImportError as error:
            raise ShadowLiveExportError(
                f"cannot publish Shadow CustomData pairs: {error}"
            ) from error
        result = {
            **published.as_dict(),
            "transport": "nampower_customdata_jsonl",
            "source": str(export_path),
            "source_signature": stable.signature_dict(),
            "changed": True,
            **diagnostics,
        }
        self._handled_signature = stable.signature()
        self._last_result = result
        return result
