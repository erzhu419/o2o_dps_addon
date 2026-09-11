"""Fail-closed selection and future-corpus admission contracts for Fury v2.

This module is intentionally independent from the rollout analyzer.  It turns
two otherwise informal boundaries into content-addressed, replayable records:

* a single preregistered selection over an exact frozen shortlist; and
* admission of the first ``N`` eligible Chronicle uploads in a closed discovery
  window after the selected candidate was externally anchored.

An anchor document is not trusted merely because it calls itself an anchor.
Validation requires its content hash to be supplied through an out-of-band
allowlist.  Consequently, artifacts built without such an allowlist remain
prepared evidence only and cannot pass final admission.

Discovery pages are never accepted as caller-authored records or hashes.  A
versioned physical capture manifest points to the retained bytes returned by
Chronicle's documented ``activities``/page-number API and to a compact physical
evidence manifest for every instance.  Validation reopens those files, parses
the provider records, and derives all artifact and guild/player-component
hashes.  The receipt keeps only hashes and normalized projections.  This makes
the captured walk replayable and tamper-evident, but it cannot cryptographically
prove that a mutable, offset-paginated provider disclosed every matching row.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from math import isfinite
import os
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import parse_qs, urlsplit
from uuid import UUID


JSONMap = dict[str, Any]

FROZEN_SHORTLIST_SCHEMA = "fury_frozen_selection_shortlist/v2"
SELECTION_RECEIPT_SCHEMA = "fury_one_time_selection_receipt/v2"
EXTERNAL_ANCHOR_SCHEMA = "fury_external_content_anchor/v1"
CANDIDATE_SEAL_SCHEMA = "fury_candidate_seal/v2"
DEVELOPMENT_EXCLUSION_SCHEMA = "fury_development_exclusion_snapshot/v2"
DISCOVERY_QUERY_SCHEMA = "chronicle_final_discovery_query/v2"
DISCOVERY_UNIVERSE_SCHEMA = "chronicle_final_discovery_universe/v2"
DISCOVERY_CAPTURE_MANIFEST_SCHEMA = (
    "chronicle_final_discovery_capture_manifest/v1"
)
INSTANCE_EVIDENCE_MANIFEST_SCHEMA = (
    "chronicle_compact_instance_evidence_manifest/v1"
)
FINAL_ADMISSION_SCHEMA = "fury_final_corpus_admission_receipt/v2"

SEED_IDENTITY_SCHEMA = "fury_seed_identity/v2"
CORPUS_IDENTITY_SCHEMA = "fury_corpus_identity/v2"
RUNNER_IDENTITY_SCHEMA = "fury_runner_identity/v2"

SELECTION_RULE_ID = "best_finite_metric_v1"
TIE_BREAK_ID = "candidate_identity_sha256_ascending_v1"
DISCOVERY_ENDPOINT = "/api/external/v1/raidlogs/recent"
DISCOVERY_PROVIDER_HOST = "capy.chronicleclassic.com"
DISCOVERY_ORDER = "uploaded_at_then_instance_id"
DISCOVERY_PROVIDER_ORDER = "started_at_desc_then_instance_id_desc"
ANCHOR_TRUST_NOTICE = "REQUIRES_OUT_OF_BAND_SEAL_SHA256_ALLOWLIST"
DISCOVERY_COMPLETENESS_BOUNDARY = (
    "PHYSICAL_PROVIDER_PAGE_REPLAY_NOT_CRYPTOGRAPHIC_COMPLETENESS_PROOF"
)
DISCOVERY_RECORD_ORIGIN = (
    "PARSED_CHRONICLE_EXTERNAL_RECENT_API_RESPONSE_V1"
)
COMPONENT_IDENTITY_SCHEMA = "chronicle_guild_player_component/v1"

_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")


class FurySelectionAdmissionV2Error(RuntimeError):
    """A selection, seal, discovery, or final-admission invariant failed."""


def canonical_json_bytes(value: Any) -> bytes:
    """Return deterministic strict-JSON bytes used by every identity below."""

    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise FurySelectionAdmissionV2Error(
            "artifact is not strict canonical JSON"
        ) from error


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FurySelectionAdmissionV2Error(f"{label} must be an object")
    return value


def _sequence(value: Any, label: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise FurySelectionAdmissionV2Error(f"{label} must be an array")
    return value


def _exact_fields(value: Mapping[str, Any], fields: set[str], label: str) -> None:
    if set(value) != fields:
        missing = sorted(fields.difference(value))
        extra = sorted(set(value).difference(fields))
        raise FurySelectionAdmissionV2Error(
            f"{label} fields mismatch: missing={missing}, extra={extra}"
        )


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise FurySelectionAdmissionV2Error(
            f"{label} must be a lowercase SHA-256"
        )
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FurySelectionAdmissionV2Error(f"{label} must be nonempty text")
    return value.strip()


def _canonical_instance_id(value: Any, label: str) -> str:
    """Normalize a canonical hyphenated UUID to lowercase.

    Case is deliberately normalized before hashing or duplicate checks so an
    uppercase spelling cannot evade a development/final-corpus exclusion.
    Other UUID spellings (braces, URNs, or omitted hyphens) are rejected rather
    than becoming a second textual identity for the same instance.
    """

    rendered = _text(value, label)
    try:
        canonical = str(UUID(rendered))
    except (ValueError, AttributeError) as error:
        raise FurySelectionAdmissionV2Error(
            f"{label} must be a canonical UUID"
        ) from error
    if rendered.lower() != canonical:
        raise FurySelectionAdmissionV2Error(
            f"{label} must use the canonical hyphenated UUID spelling"
        )
    return canonical


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise FurySelectionAdmissionV2Error(f"{label} must be a positive integer")
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FurySelectionAdmissionV2Error(
            f"{label} must be a nonnegative integer"
        )
    return value


def _utc_timestamp(value: Any, label: str) -> str:
    rendered = _text(value, label)
    try:
        parsed = datetime.fromisoformat(rendered.replace("Z", "+00:00"))
    except ValueError as error:
        raise FurySelectionAdmissionV2Error(f"{label} is not ISO-8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FurySelectionAdmissionV2Error(f"{label} must include a timezone")
    return (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _timestamp_value(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _physical_file_bytes(value: Any, label: str) -> tuple[Path, bytes, str]:
    """Read one retained physical artifact and derive its exact byte identity."""

    if isinstance(value, os.PathLike):
        rendered = os.fspath(value)
    else:
        rendered = _text(value, label)
    try:
        path = Path(rendered).expanduser().resolve(strict=True)
        if not path.is_file():
            raise OSError("path is not a regular file")
        payload = path.read_bytes()
    except OSError as error:
        raise FurySelectionAdmissionV2Error(
            f"{label} must reference a retained physical file: {error}"
        ) from error
    if not payload:
        raise FurySelectionAdmissionV2Error(f"{label} physical file is empty")
    return path, payload, hashlib.sha256(payload).hexdigest()


def _physical_json(
    value: Any,
    label: str,
) -> tuple[Path, Any, bytes, str]:
    path, payload, digest = _physical_file_bytes(value, label)
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FurySelectionAdmissionV2Error(
            f"{label} is not a UTF-8 JSON artifact"
        ) from error
    return path, decoded, payload, digest


def _relative_physical_path(base: Path, value: Any, label: str) -> Path:
    if isinstance(value, os.PathLike):
        rendered = os.fspath(value)
    else:
        rendered = _text(value, label)
    candidate = Path(rendered)
    if not candidate.is_absolute():
        candidate = base / candidate
    try:
        return candidate.resolve(strict=True)
    except OSError as error:
        raise FurySelectionAdmissionV2Error(
            f"{label} must reference a retained physical file: {error}"
        ) from error


def _unique_sorted_digests(value: Any, label: str, *, allow_empty: bool) -> list[str]:
    rows = list(_sequence(value, label))
    if not allow_empty and not rows:
        raise FurySelectionAdmissionV2Error(f"{label} must not be empty")
    normalized = [_digest(item, f"{label}[]") for item in rows]
    if normalized != sorted(set(normalized)):
        raise FurySelectionAdmissionV2Error(
            f"{label} must be sorted and unique"
        )
    return normalized


def _normalize_candidate(value: Any, label: str = "candidate") -> JSONMap:
    row = _mapping(value, label)
    fields = {
        "policy_id",
        "policy_source_sha256",
        "policy_adapter_sha256",
        "policy_profile_sha256",
    }
    _exact_fields(row, fields, label)
    core: JSONMap = {
        "policy_id": _text(row.get("policy_id"), f"{label}.policy_id"),
        "policy_source_sha256": _digest(
            row.get("policy_source_sha256"), f"{label}.policy_source_sha256"
        ),
        "policy_adapter_sha256": _digest(
            row.get("policy_adapter_sha256"), f"{label}.policy_adapter_sha256"
        ),
        "policy_profile_sha256": _digest(
            row.get("policy_profile_sha256"), f"{label}.policy_profile_sha256"
        ),
    }
    return {**core, "candidate_identity_sha256": sha256_json(core)}


def _normalize_seed_identity(value: Any, *, phase: str) -> JSONMap:
    row = _mapping(value, "seed identity")
    fields = {"schema", "phase", "seed_count", "seed_list_sha256"}
    _exact_fields(row, fields, "seed identity")
    if row.get("schema") != SEED_IDENTITY_SCHEMA:
        raise FurySelectionAdmissionV2Error("seed identity schema mismatch")
    if row.get("phase") != phase:
        raise FurySelectionAdmissionV2Error(
            f"seed identity phase must be {phase!r}"
        )
    return {
        "schema": SEED_IDENTITY_SCHEMA,
        "phase": phase,
        "seed_count": _positive_int(row.get("seed_count"), "seed identity.seed_count"),
        "seed_list_sha256": _digest(
            row.get("seed_list_sha256"), "seed identity.seed_list_sha256"
        ),
    }


def _normalize_corpus_identity(value: Any) -> JSONMap:
    row = _mapping(value, "corpus identity")
    fields = {
        "schema",
        "corpus_manifest_sha256",
        "corpus_binding_sha256",
        "source_instance_provenance_sha256",
        "scenario_count",
    }
    _exact_fields(row, fields, "corpus identity")
    if row.get("schema") != CORPUS_IDENTITY_SCHEMA:
        raise FurySelectionAdmissionV2Error("corpus identity schema mismatch")
    return {
        "schema": CORPUS_IDENTITY_SCHEMA,
        "corpus_manifest_sha256": _digest(
            row.get("corpus_manifest_sha256"),
            "corpus identity.corpus_manifest_sha256",
        ),
        "corpus_binding_sha256": _digest(
            row.get("corpus_binding_sha256"),
            "corpus identity.corpus_binding_sha256",
        ),
        "source_instance_provenance_sha256": _digest(
            row.get("source_instance_provenance_sha256"),
            "corpus identity.source_instance_provenance_sha256",
        ),
        "scenario_count": _positive_int(
            row.get("scenario_count"), "corpus identity.scenario_count"
        ),
    }


def _normalize_runner_identity(value: Any) -> JSONMap:
    row = _mapping(value, "runner identity")
    fields = {
        "schema",
        "runner_source_identity_sha256",
        "runner_inputs_sha256",
        "runner_scenario_bundle_sha256",
        "bridge_sha256",
        "execution_bundle_sha256",
    }
    _exact_fields(row, fields, "runner identity")
    if row.get("schema") != RUNNER_IDENTITY_SCHEMA:
        raise FurySelectionAdmissionV2Error("runner identity schema mismatch")
    return {
        "schema": RUNNER_IDENTITY_SCHEMA,
        "runner_source_identity_sha256": _digest(
            row.get("runner_source_identity_sha256"),
            "runner identity.runner_source_identity_sha256",
        ),
        "runner_inputs_sha256": _digest(
            row.get("runner_inputs_sha256"),
            "runner identity.runner_inputs_sha256",
        ),
        "runner_scenario_bundle_sha256": _digest(
            row.get("runner_scenario_bundle_sha256"),
            "runner identity.runner_scenario_bundle_sha256",
        ),
        "bridge_sha256": _digest(
            row.get("bridge_sha256"), "runner identity.bridge_sha256"
        ),
        "execution_bundle_sha256": _digest(
            row.get("execution_bundle_sha256"),
            "runner identity.execution_bundle_sha256",
        ),
    }


def build_frozen_selection_shortlist(
    *,
    candidates: Sequence[Mapping[str, Any]],
    metric_id: str,
    metric_definition_sha256: str,
    metric_direction: str,
    selection_once_nonce_sha256: str,
    seed_identity: Mapping[str, Any],
    corpus_identity: Mapping[str, Any],
    runner_identity: Mapping[str, Any],
) -> JSONMap:
    """Freeze the only candidates, metric, rule, tie-break, and inputs allowed."""

    normalized = [
        _normalize_candidate(value, f"candidates[{index}]")
        for index, value in enumerate(candidates)
    ]
    if not normalized:
        raise FurySelectionAdmissionV2Error("shortlist must not be empty")
    normalized.sort(key=lambda row: str(row["candidate_identity_sha256"]))
    identity_hashes = [str(row["candidate_identity_sha256"]) for row in normalized]
    policy_ids = [str(row["policy_id"]) for row in normalized]
    if len(identity_hashes) != len(set(identity_hashes)):
        raise FurySelectionAdmissionV2Error("shortlist candidate identities must be unique")
    if len(policy_ids) != len(set(policy_ids)):
        raise FurySelectionAdmissionV2Error("shortlist policy_id values must be unique")
    direction = _text(metric_direction, "metric_direction")
    if direction not in {"maximize", "minimize"}:
        raise FurySelectionAdmissionV2Error(
            "metric_direction must be 'maximize' or 'minimize'"
        )
    core: JSONMap = {
        "schema": FROZEN_SHORTLIST_SCHEMA,
        "status": "FROZEN",
        "candidates": normalized,
        "candidate_count": len(normalized),
        "candidate_identity_set_sha256": sha256_json(identity_hashes),
        "selection_contract": {
            "metric": {
                "metric_id": _text(metric_id, "metric_id"),
                "definition_sha256": _digest(
                    metric_definition_sha256, "metric_definition_sha256"
                ),
                "direction": direction,
            },
            "rule": {"rule_id": SELECTION_RULE_ID},
            "tie_break": {"tie_break_id": TIE_BREAK_ID},
            "max_selected": 1,
            "selection_once_nonce_sha256": _digest(
                selection_once_nonce_sha256,
                "selection_once_nonce_sha256",
            ),
        },
        "selection_input_identities": {
            "seed": _normalize_seed_identity(
                seed_identity, phase="selection_validation"
            ),
            "corpus": _normalize_corpus_identity(corpus_identity),
            "runner": _normalize_runner_identity(runner_identity),
        },
    }
    return {**core, "shortlist_sha256": sha256_json(core)}


def validate_frozen_selection_shortlist(value: Any) -> JSONMap:
    row = _mapping(value, "frozen shortlist")
    fields = {
        "schema",
        "status",
        "candidates",
        "candidate_count",
        "candidate_identity_set_sha256",
        "selection_contract",
        "selection_input_identities",
        "shortlist_sha256",
    }
    _exact_fields(row, fields, "frozen shortlist")
    if row.get("schema") != FROZEN_SHORTLIST_SCHEMA or row.get("status") != "FROZEN":
        raise FurySelectionAdmissionV2Error("shortlist is not a frozen v2 shortlist")
    selection = _mapping(row.get("selection_contract"), "selection_contract")
    _exact_fields(
        selection,
        {
            "metric",
            "rule",
            "tie_break",
            "max_selected",
            "selection_once_nonce_sha256",
        },
        "selection_contract",
    )
    metric = _mapping(selection.get("metric"), "selection metric")
    rule = _mapping(selection.get("rule"), "selection rule")
    tie_break = _mapping(selection.get("tie_break"), "selection tie_break")
    _exact_fields(metric, {"metric_id", "definition_sha256", "direction"}, "metric")
    _exact_fields(rule, {"rule_id"}, "rule")
    _exact_fields(tie_break, {"tie_break_id"}, "tie_break")
    if selection.get("max_selected") != 1:
        raise FurySelectionAdmissionV2Error("max_selected must be exactly 1")
    if rule.get("rule_id") != SELECTION_RULE_ID:
        raise FurySelectionAdmissionV2Error("selection rule is not preregistered")
    if tie_break.get("tie_break_id") != TIE_BREAK_ID:
        raise FurySelectionAdmissionV2Error("selection tie-break is not preregistered")
    identities = _mapping(
        row.get("selection_input_identities"), "selection_input_identities"
    )
    _exact_fields(identities, {"seed", "corpus", "runner"}, "selection identities")
    rebuilt = build_frozen_selection_shortlist(
        candidates=[
            {
                field: _mapping(item, "shortlist candidate").get(field)
                for field in (
                    "policy_id",
                    "policy_source_sha256",
                    "policy_adapter_sha256",
                    "policy_profile_sha256",
                )
            }
            for item in _sequence(row.get("candidates"), "shortlist candidates")
        ],
        metric_id=metric.get("metric_id"),
        metric_definition_sha256=metric.get("definition_sha256"),
        metric_direction=metric.get("direction"),
        selection_once_nonce_sha256=selection.get("selection_once_nonce_sha256"),
        seed_identity=_mapping(identities.get("seed"), "selection seed identity"),
        corpus_identity=_mapping(identities.get("corpus"), "selection corpus identity"),
        runner_identity=_mapping(identities.get("runner"), "selection runner identity"),
    )
    if dict(row) != rebuilt:
        raise FurySelectionAdmissionV2Error(
            "frozen shortlist is inconsistent or not content-addressed"
        )
    return rebuilt


def build_selection_receipt(
    frozen_shortlist: Mapping[str, Any],
    *,
    metric_rows: Sequence[Mapping[str, Any]],
) -> JSONMap:
    """Select once from the complete shortlist using its only registered rule."""

    shortlist = validate_frozen_selection_shortlist(frozen_shortlist)
    candidates = list(shortlist["candidates"])
    expected_hashes = {str(row["candidate_identity_sha256"]) for row in candidates}
    normalized_rows: list[JSONMap] = []
    for index, value in enumerate(metric_rows):
        row = _mapping(value, f"metric_rows[{index}]")
        _exact_fields(
            row,
            {"candidate_identity_sha256", "metric_value", "metric_evidence_sha256"},
            f"metric_rows[{index}]",
        )
        candidate_hash = _digest(
            row.get("candidate_identity_sha256"),
            f"metric_rows[{index}].candidate_identity_sha256",
        )
        metric_value = row.get("metric_value")
        if (
            isinstance(metric_value, bool)
            or not isinstance(metric_value, (int, float))
            or not isfinite(float(metric_value))
        ):
            raise FurySelectionAdmissionV2Error(
                f"metric_rows[{index}].metric_value must be finite"
            )
        normalized_rows.append(
            {
                "candidate_identity_sha256": candidate_hash,
                "metric_value": float(metric_value),
                "metric_evidence_sha256": _digest(
                    row.get("metric_evidence_sha256"),
                    f"metric_rows[{index}].metric_evidence_sha256",
                ),
            }
        )
    observed_hashes = [str(row["candidate_identity_sha256"]) for row in normalized_rows]
    if len(observed_hashes) != len(set(observed_hashes)):
        raise FurySelectionAdmissionV2Error("metric rows contain a duplicate candidate")
    if set(observed_hashes) != expected_hashes:
        missing = sorted(expected_hashes.difference(observed_hashes))
        extra = sorted(set(observed_hashes).difference(expected_hashes))
        raise FurySelectionAdmissionV2Error(
            f"metric rows must exactly cover the complete shortlist: missing={missing}, extra={extra}"
        )
    normalized_rows.sort(key=lambda row: str(row["candidate_identity_sha256"]))
    direction = shortlist["selection_contract"]["metric"]["direction"]
    if direction == "maximize":
        ranked = sorted(
            normalized_rows,
            key=lambda row: (-float(row["metric_value"]), str(row["candidate_identity_sha256"])),
        )
    else:
        ranked = sorted(
            normalized_rows,
            key=lambda row: (float(row["metric_value"]), str(row["candidate_identity_sha256"])),
        )
    selected_hash = str(ranked[0]["candidate_identity_sha256"])
    selected = next(
        dict(row) for row in candidates if row["candidate_identity_sha256"] == selected_hash
    )
    core: JSONMap = {
        "schema": SELECTION_RECEIPT_SCHEMA,
        "frozen_shortlist": shortlist,
        "selection_once_nonce_sha256": shortlist["selection_contract"][
            "selection_once_nonce_sha256"
        ],
        "complete_metric_rows": normalized_rows,
        "ranked_candidate_identity_sha256s": [
            str(row["candidate_identity_sha256"]) for row in ranked
        ],
        "selected_candidates": [selected],
    }
    return {**core, "selection_receipt_sha256": sha256_json(core)}


def validate_selection_receipt(value: Any) -> JSONMap:
    row = _mapping(value, "selection receipt")
    fields = {
        "schema",
        "frozen_shortlist",
        "selection_once_nonce_sha256",
        "complete_metric_rows",
        "ranked_candidate_identity_sha256s",
        "selected_candidates",
        "selection_receipt_sha256",
    }
    _exact_fields(row, fields, "selection receipt")
    if row.get("schema") != SELECTION_RECEIPT_SCHEMA:
        raise FurySelectionAdmissionV2Error("selection receipt schema mismatch")
    rebuilt = build_selection_receipt(
        _mapping(row.get("frozen_shortlist"), "receipt frozen shortlist"),
        metric_rows=[
            dict(_mapping(item, "receipt metric row"))
            for item in _sequence(row.get("complete_metric_rows"), "complete_metric_rows")
        ],
    )
    if dict(row) != rebuilt:
        raise FurySelectionAdmissionV2Error(
            "selection receipt is inconsistent, incomplete, or not content-addressed"
        )
    return rebuilt


def build_external_anchor_seal(
    *,
    anchored_payload_sha256: str,
    anchored_at: str,
    anchor_provider: str,
    anchor_reference: str,
    anchor_evidence_sha256: str,
) -> JSONMap:
    """Build anchor evidence; this function does not assert that it is trusted."""

    core: JSONMap = {
        "schema": EXTERNAL_ANCHOR_SCHEMA,
        "anchored_payload_sha256": _digest(
            anchored_payload_sha256, "anchored_payload_sha256"
        ),
        "anchored_at": _utc_timestamp(anchored_at, "anchored_at"),
        "anchor_provider": _text(anchor_provider, "anchor_provider"),
        "anchor_reference": _text(anchor_reference, "anchor_reference"),
        "anchor_evidence_sha256": _digest(
            anchor_evidence_sha256, "anchor_evidence_sha256"
        ),
        "trust_notice": ANCHOR_TRUST_NOTICE,
    }
    return {**core, "external_anchor_seal_sha256": sha256_json(core)}


def validate_external_anchor_seal(value: Any) -> JSONMap:
    row = _mapping(value, "external anchor seal")
    fields = {
        "schema",
        "anchored_payload_sha256",
        "anchored_at",
        "anchor_provider",
        "anchor_reference",
        "anchor_evidence_sha256",
        "trust_notice",
        "external_anchor_seal_sha256",
    }
    _exact_fields(row, fields, "external anchor seal")
    if row.get("schema") != EXTERNAL_ANCHOR_SCHEMA:
        raise FurySelectionAdmissionV2Error("external anchor schema mismatch")
    if row.get("trust_notice") != ANCHOR_TRUST_NOTICE:
        raise FurySelectionAdmissionV2Error("external anchor trust notice mismatch")
    rebuilt = build_external_anchor_seal(
        anchored_payload_sha256=row.get("anchored_payload_sha256"),
        anchored_at=row.get("anchored_at"),
        anchor_provider=row.get("anchor_provider"),
        anchor_reference=row.get("anchor_reference"),
        anchor_evidence_sha256=row.get("anchor_evidence_sha256"),
    )
    if dict(row) != rebuilt:
        raise FurySelectionAdmissionV2Error(
            "external anchor seal is inconsistent or not content-addressed"
        )
    return rebuilt


def build_candidate_seal(
    selection_receipt: Mapping[str, Any],
    external_anchor_seal: Mapping[str, Any],
) -> JSONMap:
    """Bind the selected identity to the anchor time; trust remains out-of-band."""

    selection = validate_selection_receipt(selection_receipt)
    anchor = validate_external_anchor_seal(external_anchor_seal)
    if anchor["anchored_payload_sha256"] != selection["selection_receipt_sha256"]:
        raise FurySelectionAdmissionV2Error(
            "external anchor does not anchor the selection receipt"
        )
    selected = list(selection["selected_candidates"])
    if len(selected) != 1:
        raise FurySelectionAdmissionV2Error("candidate seal requires exactly one selection")
    core: JSONMap = {
        "schema": CANDIDATE_SEAL_SCHEMA,
        "candidate_identity": dict(selected[0]),
        "frozen_at": anchor["anchored_at"],
        "selection_receipt": selection,
        "external_anchor_seal": anchor,
        "external_anchor_trust_notice": ANCHOR_TRUST_NOTICE,
    }
    return {**core, "candidate_seal_sha256": sha256_json(core)}


def validate_candidate_seal(
    value: Any,
    *,
    trusted_external_anchor_seal_sha256s: Iterable[str],
) -> JSONMap:
    row = _mapping(value, "candidate seal")
    fields = {
        "schema",
        "candidate_identity",
        "frozen_at",
        "selection_receipt",
        "external_anchor_seal",
        "external_anchor_trust_notice",
        "candidate_seal_sha256",
    }
    _exact_fields(row, fields, "candidate seal")
    if row.get("schema") != CANDIDATE_SEAL_SCHEMA:
        raise FurySelectionAdmissionV2Error("candidate seal schema mismatch")
    if row.get("external_anchor_trust_notice") != ANCHOR_TRUST_NOTICE:
        raise FurySelectionAdmissionV2Error("candidate seal trust notice mismatch")
    rebuilt = build_candidate_seal(
        _mapping(row.get("selection_receipt"), "candidate selection receipt"),
        _mapping(row.get("external_anchor_seal"), "candidate external anchor"),
    )
    if dict(row) != rebuilt:
        raise FurySelectionAdmissionV2Error(
            "candidate seal is inconsistent or not content-addressed"
        )
    trusted = {
        _digest(item, "trusted_external_anchor_seal_sha256s[]")
        for item in trusted_external_anchor_seal_sha256s
    }
    anchor_hash = str(rebuilt["external_anchor_seal"]["external_anchor_seal_sha256"])
    if anchor_hash not in trusted:
        raise FurySelectionAdmissionV2Error(
            "external anchor is not present in the out-of-band trusted seal allowlist"
        )
    return rebuilt


def build_development_exclusion_snapshot(
    development_instances: Sequence[Mapping[str, Any]],
) -> JSONMap:
    """Content-address development instance IDs and leakage-component hashes."""

    instance_hashes: set[str] = set()
    component_hashes: set[str] = set()
    if not development_instances:
        raise FurySelectionAdmissionV2Error(
            "development exclusion snapshot must contain instances"
        )
    for index, value in enumerate(development_instances):
        row = _mapping(value, f"development_instances[{index}]")
        _exact_fields(
            row,
            {"instance_id", "guild_player_component_sha256s"},
            f"development_instances[{index}]",
        )
        instance_id = _canonical_instance_id(
            row.get("instance_id"),
            f"development_instances[{index}].instance_id",
        )
        instance_digest = hashlib.sha256(instance_id.encode("utf-8")).hexdigest()
        if instance_digest in instance_hashes:
            raise FurySelectionAdmissionV2Error("duplicate development instance_id")
        instance_hashes.add(instance_digest)
        components = _unique_sorted_digests(
            row.get("guild_player_component_sha256s"),
            f"development_instances[{index}].guild_player_component_sha256s",
            allow_empty=False,
        )
        component_hashes.update(components)
    core: JSONMap = {
        "schema": DEVELOPMENT_EXCLUSION_SCHEMA,
        "development_instance_count": len(instance_hashes),
        "development_instance_id_sha256s": sorted(instance_hashes),
        "development_guild_player_component_sha256s": sorted(component_hashes),
    }
    return {**core, "development_exclusion_sha256": sha256_json(core)}


def validate_development_exclusion_snapshot(value: Any) -> JSONMap:
    row = _mapping(value, "development exclusion snapshot")
    fields = {
        "schema",
        "development_instance_count",
        "development_instance_id_sha256s",
        "development_guild_player_component_sha256s",
        "development_exclusion_sha256",
    }
    _exact_fields(row, fields, "development exclusion snapshot")
    if row.get("schema") != DEVELOPMENT_EXCLUSION_SCHEMA:
        raise FurySelectionAdmissionV2Error("development exclusion schema mismatch")
    instances = _unique_sorted_digests(
        row.get("development_instance_id_sha256s"),
        "development_instance_id_sha256s",
        allow_empty=False,
    )
    components = _unique_sorted_digests(
        row.get("development_guild_player_component_sha256s"),
        "development_guild_player_component_sha256s",
        allow_empty=False,
    )
    if row.get("development_instance_count") != len(instances):
        raise FurySelectionAdmissionV2Error("development instance count mismatch")
    core: JSONMap = {
        "schema": DEVELOPMENT_EXCLUSION_SCHEMA,
        "development_instance_count": len(instances),
        "development_instance_id_sha256s": instances,
        "development_guild_player_component_sha256s": components,
    }
    rebuilt = {**core, "development_exclusion_sha256": sha256_json(core)}
    if dict(row) != rebuilt:
        raise FurySelectionAdmissionV2Error(
            "development exclusion is inconsistent or not content-addressed"
        )
    return rebuilt


def _normalize_discovery_query(value: Any, *, candidate_frozen_at: str) -> JSONMap:
    row = _mapping(value, "discovery query")
    fields = {
        "schema",
        "endpoint",
        "instance_name",
        "uploaded_after_exclusive",
        "uploaded_before_inclusive",
        "order",
        "page_size",
        "take_first_n",
        "require_complete_nine_bosses_plus_all_activity",
    }
    _exact_fields(row, fields, "discovery query")
    if row.get("schema") != DISCOVERY_QUERY_SCHEMA:
        raise FurySelectionAdmissionV2Error("discovery query schema mismatch")
    if row.get("endpoint") != DISCOVERY_ENDPOINT:
        raise FurySelectionAdmissionV2Error("discovery query endpoint mismatch")
    if row.get("order") != DISCOVERY_ORDER:
        raise FurySelectionAdmissionV2Error("discovery order must be preregistered")
    if row.get("require_complete_nine_bosses_plus_all_activity") is not True:
        raise FurySelectionAdmissionV2Error("final discovery must require complete raids")
    after = _utc_timestamp(row.get("uploaded_after_exclusive"), "uploaded_after_exclusive")
    before = _utc_timestamp(row.get("uploaded_before_inclusive"), "uploaded_before_inclusive")
    frozen = _utc_timestamp(candidate_frozen_at, "candidate_frozen_at")
    if after != frozen:
        raise FurySelectionAdmissionV2Error(
            "discovery window must start exclusively at candidate frozen_at"
        )
    if _timestamp_value(before) <= _timestamp_value(after):
        raise FurySelectionAdmissionV2Error(
            "discovery window must close after the candidate seal"
        )
    return {
        "schema": DISCOVERY_QUERY_SCHEMA,
        "endpoint": DISCOVERY_ENDPOINT,
        "instance_name": _text(row.get("instance_name"), "instance_name"),
        "uploaded_after_exclusive": after,
        "uploaded_before_inclusive": before,
        "order": DISCOVERY_ORDER,
        "page_size": _positive_int(row.get("page_size"), "page_size"),
        "take_first_n": _positive_int(row.get("take_first_n"), "take_first_n"),
        "require_complete_nine_bosses_plus_all_activity": True,
    }


def build_final_discovery_query(
    *,
    candidate_frozen_at: str,
    instance_name: str,
    uploaded_before_inclusive: str,
    page_size: int,
    take_first_n: int,
) -> JSONMap:
    """Build the normalized closed-window query used by final admission."""

    return _normalize_discovery_query(
        {
            "schema": DISCOVERY_QUERY_SCHEMA,
            "endpoint": DISCOVERY_ENDPOINT,
            "instance_name": instance_name,
            "uploaded_after_exclusive": candidate_frozen_at,
            "uploaded_before_inclusive": uploaded_before_inclusive,
            "order": DISCOVERY_ORDER,
            "page_size": page_size,
            "take_first_n": take_first_n,
            "require_complete_nine_bosses_plus_all_activity": True,
        },
        candidate_frozen_at=candidate_frozen_at,
    )


def _required_allowed_fields(
    value: Mapping[str, Any],
    *,
    required: set[str],
    allowed: set[str],
    label: str,
) -> None:
    missing = sorted(required.difference(value))
    extra = sorted(set(value).difference(allowed))
    if missing or extra:
        raise FurySelectionAdmissionV2Error(
            f"{label} fields mismatch: missing={missing}, extra={extra}"
        )


def _normalize_recent_activity(value: Any, label: str) -> JSONMap:
    """Parse one activity from Chronicle's documented external recent API."""

    row = _mapping(value, label)
    required = {
        "id",
        "slug",
        "name",
        "realm",
        "uploaded_at",
        "started_at",
        "ended_at",
        "player_count",
        "boss_count",
        "boss_kills",
        "has_youtube_video",
    }
    allowed = required | {
        "guild",
        "difficulty",
        "max_players",
        "recorder_name",
    }
    _required_allowed_fields(row, required=required, allowed=allowed, label=label)
    realm = _mapping(row.get("realm"), f"{label}.realm")
    _required_allowed_fields(
        realm,
        required={"id", "server_id", "name"},
        allowed={"id", "server_id", "name", "description", "url"},
        label=f"{label}.realm",
    )
    guild_id: str | None = None
    if row.get("guild") is not None:
        guild = _mapping(row.get("guild"), f"{label}.guild")
        _exact_fields(guild, {"id", "name"}, f"{label}.guild")
        guild_id = _canonical_instance_id(guild.get("id"), f"{label}.guild.id")
        _text(guild.get("name"), f"{label}.guild.name")
    has_video = row.get("has_youtube_video")
    if not isinstance(has_video, bool):
        raise FurySelectionAdmissionV2Error(
            f"{label}.has_youtube_video must be boolean"
        )
    # Validate optional values when Chronicle emitted them.  They are retained
    # by the exact raw-response hash, while the admission projection only keeps
    # fields that determine identity, order, and completeness.
    for optional_text in ("difficulty", "recorder_name"):
        if optional_text in row:
            _text(row.get(optional_text), f"{label}.{optional_text}")
    if "max_players" in row:
        _nonnegative_int(row.get("max_players"), f"{label}.max_players")
    _text(row.get("slug"), f"{label}.slug")
    boss_count = _nonnegative_int(row.get("boss_count"), f"{label}.boss_count")
    boss_kills = _nonnegative_int(row.get("boss_kills"), f"{label}.boss_kills")
    if boss_kills > boss_count:
        raise FurySelectionAdmissionV2Error(
            f"{label}.boss_kills must not exceed boss_count"
        )
    started_at = _utc_timestamp(row.get("started_at"), f"{label}.started_at")
    ended_at = _utc_timestamp(row.get("ended_at"), f"{label}.ended_at")
    if _timestamp_value(ended_at) < _timestamp_value(started_at):
        raise FurySelectionAdmissionV2Error(
            f"{label}.ended_at must not precede started_at"
        )
    return {
        "instance_id": _canonical_instance_id(row.get("id"), f"{label}.id"),
        "instance_name": _text(row.get("name"), f"{label}.name"),
        "uploaded_at": _utc_timestamp(
            row.get("uploaded_at"), f"{label}.uploaded_at"
        ),
        "started_at": started_at,
        "ended_at": ended_at,
        "realm_id": _canonical_instance_id(realm.get("id"), f"{label}.realm.id"),
        "guild_id": guild_id,
        "player_count": _nonnegative_int(
            row.get("player_count"), f"{label}.player_count"
        ),
        "boss_count": boss_count,
        "boss_kills": boss_kills,
        "recent_activity_sha256": sha256_json(dict(row)),
    }


def _component_sha256s_from_instance_metadata(
    metadata: Mapping[str, Any],
    *,
    activity: Mapping[str, Any],
    label: str,
) -> list[str]:
    metadata_instance_id = _canonical_instance_id(
        metadata.get("id"), f"{label}.id"
    )
    if metadata_instance_id != activity["instance_id"]:
        raise FurySelectionAdmissionV2Error(
            f"{label}.id does not match the provider recent activity"
        )
    metadata_name = _text(metadata.get("name"), f"{label}.name")
    if metadata_name != activity["instance_name"]:
        raise FurySelectionAdmissionV2Error(
            f"{label}.name does not match the provider recent activity"
        )
    realm_id = _canonical_instance_id(
        metadata.get("realm_id"), f"{label}.realm_id"
    )
    if realm_id != activity["realm_id"]:
        raise FurySelectionAdmissionV2Error(
            f"{label}.realm_id does not match the provider recent activity"
        )

    guild_id: str | None = None
    if metadata.get("guild") is not None:
        guild = _mapping(metadata.get("guild"), f"{label}.guild")
        guild_id = _canonical_instance_id(
            guild.get("id"), f"{label}.guild.id"
        )
    if guild_id is not None and activity["guild_id"] is not None:
        if guild_id != activity["guild_id"]:
            raise FurySelectionAdmissionV2Error(
                f"{label}.guild.id does not match the provider recent activity"
            )
    resolved_guild_id = guild_id or activity["guild_id"]

    players = _mapping(metadata.get("players"), f"{label}.players")
    if not players:
        raise FurySelectionAdmissionV2Error(f"{label}.players must not be empty")
    normalized_player_ids: list[str] = []
    for player_guid, player_value in players.items():
        normalized_player_ids.append(
            _text(player_guid, f"{label}.players key").casefold()
        )
        _mapping(player_value, f"{label}.players[{player_guid!r}]")
    if len(normalized_player_ids) != len(set(normalized_player_ids)):
        raise FurySelectionAdmissionV2Error(
            f"{label}.players contains case-aliased duplicate player GUIDs"
        )
    if len(normalized_player_ids) != activity["player_count"]:
        raise FurySelectionAdmissionV2Error(
            f"{label}.players count does not match the provider recent activity"
        )

    identities: list[JSONMap] = []
    if resolved_guild_id is not None:
        identities.append(
            {
                "schema": COMPONENT_IDENTITY_SCHEMA,
                "kind": "GUILD",
                "realm_id": realm_id,
                "guild_id": resolved_guild_id,
            }
        )
    identities.extend(
        {
            "schema": COMPONENT_IDENTITY_SCHEMA,
            "kind": "PLAYER",
            "realm_id": realm_id,
            "player_guid": player_guid,
        }
        for player_guid in sorted(normalized_player_ids)
    )
    return sorted({sha256_json(identity) for identity in identities})


def _boss_coverage_from_instance_metadata(
    metadata: Mapping[str, Any], label: str
) -> tuple[int, int]:
    encounters = _sequence(metadata.get("encounters"), f"{label}.encounters")
    boss_count = 0
    boss_kills = 0
    for index, value in enumerate(encounters):
        encounter = _mapping(value, f"{label}.encounters[{index}]")
        boss = encounter.get("boss")
        if not isinstance(boss, bool):
            raise FurySelectionAdmissionV2Error(
                f"{label}.encounters[{index}].boss must be boolean"
            )
        if boss:
            boss_count += 1
            if encounter.get("kill_type") in {"clean", "partial"}:
                boss_kills += 1
    return boss_count, boss_kills


def _record_from_physical_instance_evidence(
    value: Any,
    *,
    base: Path,
    activity: Mapping[str, Any],
    label: str,
) -> JSONMap:
    manifest_path = _relative_physical_path(base, value, label)
    resolved_path, manifest_value, _, manifest_sha256 = _physical_json(
        manifest_path, label
    )
    manifest = _mapping(manifest_value, label)
    _exact_fields(
        manifest,
        {
            "schema",
            "instance_id",
            "instance_metadata_path",
            "ranking_records_path",
            "all_activity_stream_path",
        },
        label,
    )
    if manifest.get("schema") != INSTANCE_EVIDENCE_MANIFEST_SCHEMA:
        raise FurySelectionAdmissionV2Error(
            f"{label} compact instance evidence schema mismatch"
        )
    instance_id = _canonical_instance_id(
        manifest.get("instance_id"), f"{label}.instance_id"
    )
    if instance_id != activity["instance_id"]:
        raise FurySelectionAdmissionV2Error(
            f"{label}.instance_id does not match the provider recent activity"
        )

    artifact_base = resolved_path.parent
    metadata_path = _relative_physical_path(
        artifact_base,
        manifest.get("instance_metadata_path"),
        f"{label}.instance_metadata_path",
    )
    _, metadata_value, _, metadata_sha256 = _physical_json(
        metadata_path, f"{label}.instance_metadata_path"
    )
    metadata = _mapping(metadata_value, f"{label}.instance_metadata")
    components = _component_sha256s_from_instance_metadata(
        metadata, activity=activity, label=f"{label}.instance_metadata"
    )
    detail_boss_count, detail_boss_kills = _boss_coverage_from_instance_metadata(
        metadata, f"{label}.instance_metadata"
    )
    if detail_boss_count != activity["boss_count"]:
        raise FurySelectionAdmissionV2Error(
            f"{label} boss_count disagrees between recent and instance metadata"
        )
    if detail_boss_kills != activity["boss_kills"]:
        raise FurySelectionAdmissionV2Error(
            f"{label} boss_kills disagrees between recent and instance metadata"
        )

    rankings_path = _relative_physical_path(
        artifact_base,
        manifest.get("ranking_records_path"),
        f"{label}.ranking_records_path",
    )
    _, ranking_value, _, ranking_sha256 = _physical_json(
        rankings_path, f"{label}.ranking_records_path"
    )
    rankings = _sequence(ranking_value, f"{label}.ranking_records")
    for index, ranking in enumerate(rankings):
        _mapping(ranking, f"{label}.ranking_records[{index}]")

    activity_stream_path = _relative_physical_path(
        artifact_base,
        manifest.get("all_activity_stream_path"),
        f"{label}.all_activity_stream_path",
    )
    _, activity_stream_bytes, activity_stream_sha256 = _physical_file_bytes(
        activity_stream_path, f"{label}.all_activity_stream_path"
    )
    complete = (
        detail_boss_count == 9
        and detail_boss_kills == 9
        and bool(rankings)
        and bool(activity_stream_bytes)
    )
    return {
        "instance_id": instance_id,
        "instance_name": str(activity["instance_name"]),
        "uploaded_at": str(activity["uploaded_at"]),
        "complete_nine_bosses_plus_all_activity": complete,
        "recent_activity_sha256": str(activity["recent_activity_sha256"]),
        "compact_instance_evidence_manifest_sha256": manifest_sha256,
        "instance_metadata_sha256": metadata_sha256,
        "ranking_records_sha256": ranking_sha256,
        "all_activity_stream_sha256": activity_stream_sha256,
        "component_identity_schema": COMPONENT_IDENTITY_SCHEMA,
        "guild_player_component_sha256s": components,
    }


def _validate_recent_request_target(
    value: Any,
    *,
    query: Mapping[str, Any],
    expected_page: int,
    label: str,
) -> str:
    rendered = _text(value, label)
    split = urlsplit(rendered)
    if split.fragment:
        raise FurySelectionAdmissionV2Error(f"{label} must not contain a fragment")
    if split.scheme or split.netloc:
        if split.scheme != "https" or split.netloc.casefold() != DISCOVERY_PROVIDER_HOST:
            raise FurySelectionAdmissionV2Error(
                f"{label} absolute URL must use the registered Chronicle HTTPS host"
            )
    if split.path != DISCOVERY_ENDPOINT:
        raise FurySelectionAdmissionV2Error(
            f"{label} path must equal the registered Chronicle endpoint"
        )
    try:
        parameters = parse_qs(
            split.query, keep_blank_values=True, strict_parsing=True
        )
    except ValueError as error:
        raise FurySelectionAdmissionV2Error(
            f"{label} contains a malformed query string"
        ) from error
    expected_fields = {"upload_after", "instance_name", "page", "page_size"}
    if set(parameters) != expected_fields or any(
        len(items) != 1 for items in parameters.values()
    ):
        raise FurySelectionAdmissionV2Error(
            f"{label} must contain exactly one upload_after, instance_name, page, "
            "and page_size parameter"
        )
    upload_after = _utc_timestamp(parameters["upload_after"][0], f"{label}.upload_after")
    if upload_after != query["uploaded_after_exclusive"]:
        raise FurySelectionAdmissionV2Error(
            f"{label}.upload_after does not match the frozen discovery query"
        )
    if parameters["instance_name"][0] != query["instance_name"]:
        raise FurySelectionAdmissionV2Error(
            f"{label}.instance_name does not match the frozen discovery query"
        )
    try:
        page = int(parameters["page"][0])
        page_size = int(parameters["page_size"][0])
    except ValueError as error:
        raise FurySelectionAdmissionV2Error(
            f"{label} page and page_size must be integers"
        ) from error
    if page != expected_page or page_size != query["page_size"]:
        raise FurySelectionAdmissionV2Error(
            f"{label} page/page_size does not match the physical page sequence"
        )
    return rendered


def _parse_recent_response(
    value: Any,
    *,
    expected_page: int,
    page_size: int,
    label: str,
) -> tuple[list[JSONMap], bool]:
    response = _mapping(value, label)
    _exact_fields(response, {"activities", "pagination"}, label)
    pagination = _mapping(response.get("pagination"), f"{label}.pagination")
    _exact_fields(
        pagination, {"page", "page_size", "has_more"}, f"{label}.pagination"
    )
    if pagination.get("page") != expected_page:
        raise FurySelectionAdmissionV2Error(
            f"{label}.pagination.page is not contiguous"
        )
    if pagination.get("page_size") != page_size:
        raise FurySelectionAdmissionV2Error(
            f"{label}.pagination.page_size does not match the frozen query"
        )
    has_more = pagination.get("has_more")
    if not isinstance(has_more, bool):
        raise FurySelectionAdmissionV2Error(
            f"{label}.pagination.has_more must be boolean"
        )
    activities = [
        _normalize_recent_activity(item, f"{label}.activities[{index}]")
        for index, item in enumerate(
            _sequence(response.get("activities"), f"{label}.activities")
        )
    ]
    if has_more and len(activities) != page_size:
        raise FurySelectionAdmissionV2Error(
            "nonterminal Chronicle response must contain the requested page_size"
        )
    if len(activities) > page_size:
        raise FurySelectionAdmissionV2Error(
            "Chronicle response exceeds the requested page_size"
        )
    return activities, has_more


def replay_final_discovery_capture(
    discovery_query: Mapping[str, Any],
    *,
    candidate_frozen_at: str,
    discovery_capture_manifest_path: str | os.PathLike[str],
) -> JSONMap:
    """Replay retained provider bytes and compact per-instance evidence.

    The input is one physical, versioned capture manifest.  Its page entries
    contain paths only: record values and every digest in the returned universe
    are recomputed from the retained Chronicle responses and instance evidence.
    No path or raw response body is copied into the final receipt.
    """

    query = _normalize_discovery_query(
        discovery_query, candidate_frozen_at=candidate_frozen_at
    )
    manifest_path, manifest_value, _, manifest_sha256 = _physical_json(
        discovery_capture_manifest_path, "discovery capture manifest"
    )
    manifest = _mapping(manifest_value, "discovery capture manifest")
    _exact_fields(
        manifest,
        {"schema", "query_sha256", "retrieved_at", "pages"},
        "discovery capture manifest",
    )
    if manifest.get("schema") != DISCOVERY_CAPTURE_MANIFEST_SCHEMA:
        raise FurySelectionAdmissionV2Error(
            "discovery capture manifest schema mismatch"
        )
    expected_query_hash = sha256_json(query)
    if manifest.get("query_sha256") != expected_query_hash:
        raise FurySelectionAdmissionV2Error(
            "discovery capture manifest query identity mismatch"
        )
    retrieved_at = _utc_timestamp(
        manifest.get("retrieved_at"), "discovery capture retrieved_at"
    )
    if _timestamp_value(retrieved_at) < _timestamp_value(
        str(query["uploaded_before_inclusive"])
    ):
        raise FurySelectionAdmissionV2Error(
            "discovery was retrieved before the closed window ended"
        )

    page_entries = list(_sequence(manifest.get("pages"), "capture pages"))
    if not page_entries:
        raise FurySelectionAdmissionV2Error(
            "discovery capture must include a terminal provider page"
        )
    pages: list[JSONMap] = []
    seen_ids: set[str] = set()
    previous_provider_key: tuple[datetime, str] | None = None
    manifest_base = manifest_path.parent
    page_size = int(query["page_size"])
    for index, value_page in enumerate(page_entries):
        label = f"capture pages[{index}]"
        entry = _mapping(value_page, label)
        _exact_fields(
            entry,
            {
                "request_target",
                "response_path",
                "instance_evidence_manifest_paths",
            },
            label,
        )
        provider_page = index + 1
        request_target = _validate_recent_request_target(
            entry.get("request_target"),
            query=query,
            expected_page=provider_page,
            label=f"{label}.request_target",
        )
        response_path = _relative_physical_path(
            manifest_base, entry.get("response_path"), f"{label}.response_path"
        )
        _, response_value, response_bytes, response_sha256 = _physical_json(
            response_path, f"{label}.response_path"
        )
        activities, has_more = _parse_recent_response(
            response_value,
            expected_page=provider_page,
            page_size=page_size,
            label=f"{label}.response",
        )
        for activity in activities:
            provider_key = (
                _timestamp_value(str(activity["started_at"])),
                str(activity["instance_id"]),
            )
            if (
                previous_provider_key is not None
                and provider_key > previous_provider_key
            ):
                raise FurySelectionAdmissionV2Error(
                    "Chronicle recent activities do not follow the documented "
                    "started_at DESC, instance_id DESC provider order"
                )
            previous_provider_key = provider_key
        evidence_paths = list(
            _sequence(
                entry.get("instance_evidence_manifest_paths"),
                f"{label}.instance_evidence_manifest_paths",
            )
        )
        if len(evidence_paths) != len(activities):
            raise FurySelectionAdmissionV2Error(
                f"{label} must retain exactly one instance evidence manifest "
                "for each provider activity"
            )
        records_by_id: dict[str, JSONMap] = {}
        activities_by_id = {str(item["instance_id"]): item for item in activities}
        if len(activities_by_id) != len(activities):
            raise FurySelectionAdmissionV2Error(
                "one Chronicle response page contains duplicate instance IDs"
            )
        for evidence_index, evidence_path in enumerate(evidence_paths):
            # Read the manifest once to identify its instance, then replay it
            # against the provider-derived activity.  The helper re-reads and
            # hashes it, which also catches replacement between lookup and use.
            resolved_evidence = _relative_physical_path(
                manifest_base,
                evidence_path,
                f"{label}.instance_evidence_manifest_paths[{evidence_index}]",
            )
            _, preview_value, _, _ = _physical_json(
                resolved_evidence,
                f"{label}.instance_evidence_manifest_paths[{evidence_index}]",
            )
            preview = _mapping(
                preview_value,
                f"{label}.instance_evidence_manifest_paths[{evidence_index}]",
            )
            instance_id = _canonical_instance_id(
                preview.get("instance_id"),
                f"{label}.instance_evidence_manifest_paths[{evidence_index}].instance_id",
            )
            if instance_id in records_by_id:
                raise FurySelectionAdmissionV2Error(
                    f"{label} contains duplicate instance evidence manifests"
                )
            if instance_id not in activities_by_id:
                raise FurySelectionAdmissionV2Error(
                    f"{label} instance evidence does not match a provider activity"
                )
            records_by_id[instance_id] = _record_from_physical_instance_evidence(
                resolved_evidence,
                base=manifest_base,
                activity=activities_by_id[instance_id],
                label=f"{label}.instance_evidence[{instance_id}]",
            )
        if set(records_by_id) != set(activities_by_id):
            raise FurySelectionAdmissionV2Error(
                f"{label} physical instance evidence coverage is incomplete"
            )
        records = [records_by_id[str(item["instance_id"])] for item in activities]
        for record in records:
            instance_id = str(record["instance_id"])
            if instance_id in seen_ids:
                raise FurySelectionAdmissionV2Error(
                    "discovery capture contains a duplicate instance_id across pages"
                )
            seen_ids.add(instance_id)
        pages.append(
            {
                "page_index": index,
                "provider_page": provider_page,
                "provider_order": DISCOVERY_PROVIDER_ORDER,
                "has_more": has_more,
                "request_target_sha256": hashlib.sha256(
                    request_target.encode("utf-8")
                ).hexdigest(),
                "raw_response_sha256": response_sha256,
                "raw_response_size_bytes": len(response_bytes),
                "record_origin": DISCOVERY_RECORD_ORIGIN,
                "records": records,
            }
        )
        if index < len(page_entries) - 1 and not has_more:
            raise FurySelectionAdmissionV2Error(
                "capture contains a page after Chronicle declared pagination terminal"
            )
    if pages[-1]["has_more"] is not False:
        raise FurySelectionAdmissionV2Error(
            "discovery pagination is incomplete: terminal has_more is true"
        )
    return {
        "schema": DISCOVERY_UNIVERSE_SCHEMA,
        "capture_manifest_schema": DISCOVERY_CAPTURE_MANIFEST_SCHEMA,
        "capture_manifest_sha256": manifest_sha256,
        "query_sha256": expected_query_hash,
        "retrieved_at": retrieved_at,
        "completeness_boundary": DISCOVERY_COMPLETENESS_BOUNDARY,
        "pages": pages,
    }


def _selected_source_instance_provenance(
    selected_ledger_rows: Sequence[Mapping[str, Any]],
) -> tuple[list[JSONMap], str]:
    """Return the canonical per-instance source ledger and its content hash."""

    provenance = [
        {
            "instance_id": str(row["instance"]["instance_id"]),
            "recent_activity_sha256": str(
                row["instance"]["recent_activity_sha256"]
            ),
            "compact_instance_evidence_manifest_sha256": str(
                row["instance"]["compact_instance_evidence_manifest_sha256"]
            ),
            "instance_metadata_sha256": str(
                row["instance"]["instance_metadata_sha256"]
            ),
            "ranking_records_sha256": str(
                row["instance"]["ranking_records_sha256"]
            ),
            "all_activity_stream_sha256": str(
                row["instance"]["all_activity_stream_sha256"]
            ),
            "guild_player_component_sha256s": list(
                row["instance"]["guild_player_component_sha256s"]
            ),
        }
        for row in selected_ledger_rows
    ]
    provenance.sort(key=lambda row: str(row["instance_id"]))
    return provenance, sha256_json(provenance)


def _eligibility_ledger(
    universe: Mapping[str, Any],
    *,
    query: Mapping[str, Any],
    exclusion: Mapping[str, Any],
) -> list[JSONMap]:
    development_instances = set(exclusion["development_instance_id_sha256s"])
    development_components = set(
        exclusion["development_guild_player_component_sha256s"]
    )
    after = _timestamp_value(str(query["uploaded_after_exclusive"]))
    before = _timestamp_value(str(query["uploaded_before_inclusive"]))
    records = [record for page in universe["pages"] for record in page["records"]]
    records.sort(key=lambda row: (str(row["uploaded_at"]), str(row["instance_id"])))
    ledger: list[JSONMap] = []
    for record in records:
        reasons: list[str] = []
        uploaded = _timestamp_value(str(record["uploaded_at"]))
        if record["instance_name"] != query["instance_name"]:
            reasons.append("INSTANCE_NAME_MISMATCH")
        if uploaded <= after:
            reasons.append("NOT_AFTER_CANDIDATE_SEAL")
        if uploaded > before:
            reasons.append("AFTER_CLOSED_WINDOW")
        if record["complete_nine_bosses_plus_all_activity"] is not True:
            reasons.append("INCOMPLETE_NINE_BOSSES_OR_ALL_ACTIVITY")
        for field, reason in (
            ("instance_metadata_sha256", "MISSING_INSTANCE_METADATA"),
            ("ranking_records_sha256", "MISSING_RANKING_RECORDS"),
            ("all_activity_stream_sha256", "MISSING_ALL_ACTIVITY_STREAM"),
        ):
            if record[field] is None:
                reasons.append(reason)
        instance_hash = hashlib.sha256(str(record["instance_id"]).encode("utf-8")).hexdigest()
        if instance_hash in development_instances:
            reasons.append("DEVELOPMENT_INSTANCE_OVERLAP")
        overlapping_components = sorted(
            set(record["guild_player_component_sha256s"]).intersection(
                development_components
            )
        )
        if overlapping_components:
            reasons.append("DEVELOPMENT_GUILD_PLAYER_COMPONENT_OVERLAP")
        ledger.append(
            {
                "instance": dict(record),
                "instance_id_sha256": instance_hash,
                "eligible": not reasons,
                "exclusion_reasons": reasons,
                "overlapping_development_component_sha256s": overlapping_components,
            }
        )
    return ledger


def build_final_corpus_admission_receipt(
    *,
    candidate_seal: Mapping[str, Any],
    trusted_external_anchor_seal_sha256s: Iterable[str],
    discovery_query: Mapping[str, Any],
    discovery_capture_manifest_path: str | os.PathLike[str] | None = None,
    discovery_universe: Mapping[str, Any] | None = None,
    development_exclusion_snapshot: Mapping[str, Any],
    selected_instance_ids: Sequence[str],
    final_seed_identity: Mapping[str, Any],
    final_corpus_identity: Mapping[str, Any],
    final_runner_identity: Mapping[str, Any],
) -> JSONMap:
    """Admit exactly the first-N eligible uploads from one closed API window."""

    candidate = validate_candidate_seal(
        candidate_seal,
        trusted_external_anchor_seal_sha256s=trusted_external_anchor_seal_sha256s,
    )
    query = _normalize_discovery_query(
        discovery_query, candidate_frozen_at=str(candidate["frozen_at"])
    )
    if discovery_universe is not None:
        raise FurySelectionAdmissionV2Error(
            "caller-provided discovery records and hashes are forbidden; supply "
            "discovery_capture_manifest_path for physical replay"
        )
    if discovery_capture_manifest_path is None:
        raise FurySelectionAdmissionV2Error(
            "final admission requires discovery_capture_manifest_path"
        )
    universe = replay_final_discovery_capture(
        query,
        candidate_frozen_at=str(candidate["frozen_at"]),
        discovery_capture_manifest_path=discovery_capture_manifest_path,
    )
    exclusion = validate_development_exclusion_snapshot(
        development_exclusion_snapshot
    )
    ledger = _eligibility_ledger(universe, query=query, exclusion=exclusion)
    eligible = [row for row in ledger if row["eligible"]]
    take_first_n = int(query["take_first_n"])
    if len(eligible) < take_first_n:
        raise FurySelectionAdmissionV2Error(
            f"closed discovery universe has only {len(eligible)} eligible instances; "
            f"take_first_n={take_first_n}"
        )
    expected_selected = [
        str(row["instance"]["instance_id"]) for row in eligible[:take_first_n]
    ]
    proposed = [
        _canonical_instance_id(value, f"selected_instance_ids[{index}]")
        for index, value in enumerate(selected_instance_ids)
    ]
    if len(proposed) != len(set(proposed)):
        raise FurySelectionAdmissionV2Error("selected_instance_ids must be unique")
    if proposed != expected_selected:
        raise FurySelectionAdmissionV2Error(
            "selected instances do not equal the first-N eligible uploads in "
            "uploaded_at then instance_id order; an earlier instance was skipped"
        )
    selected_ledger_rows = eligible[:take_first_n]
    provenance, provenance_sha256 = _selected_source_instance_provenance(
        selected_ledger_rows
    )
    corpus_identity = _normalize_corpus_identity(final_corpus_identity)
    if (
        corpus_identity["source_instance_provenance_sha256"]
        != provenance_sha256
    ):
        raise FurySelectionAdmissionV2Error(
            "final corpus identity does not match the selected source-instance "
            "provenance ledger"
        )
    core: JSONMap = {
        "schema": FINAL_ADMISSION_SCHEMA,
        "candidate_seal": candidate,
        "development_exclusion_snapshot": exclusion,
        "discovery_query": query,
        "complete_discovery_universe": universe,
        "complete_discovery_universe_sha256": sha256_json(universe),
        "eligibility_ledger": ledger,
        "selection_replay": {
            "order": DISCOVERY_ORDER,
            "take_first_n": take_first_n,
            "selected_instances": [
                dict(row["instance"]) for row in selected_ledger_rows
            ],
        },
        "selected_source_instance_provenance": {
            "order": "instance_id_ascending",
            "entries": provenance,
            "source_instance_provenance_sha256": provenance_sha256,
        },
        "final_input_identities": {
            "seed": _normalize_seed_identity(
                final_seed_identity, phase="final_confirmation"
            ),
            "corpus": corpus_identity,
            "runner": _normalize_runner_identity(final_runner_identity),
        },
    }
    return {**core, "final_admission_receipt_sha256": sha256_json(core)}


def validate_final_corpus_admission_receipt(
    value: Any,
    *,
    trusted_external_anchor_seal_sha256s: Iterable[str],
    discovery_capture_manifest_path: str | os.PathLike[str] | None = None,
) -> JSONMap:
    row = _mapping(value, "final admission receipt")
    fields = {
        "schema",
        "candidate_seal",
        "development_exclusion_snapshot",
        "discovery_query",
        "complete_discovery_universe",
        "complete_discovery_universe_sha256",
        "eligibility_ledger",
        "selection_replay",
        "selected_source_instance_provenance",
        "final_input_identities",
        "final_admission_receipt_sha256",
    }
    _exact_fields(row, fields, "final admission receipt")
    if row.get("schema") != FINAL_ADMISSION_SCHEMA:
        raise FurySelectionAdmissionV2Error("final admission schema mismatch")
    replay = _mapping(row.get("selection_replay"), "selection_replay")
    _exact_fields(replay, {"order", "take_first_n", "selected_instances"}, "selection_replay")
    selected_instance_ids = [
        _mapping(item, "selected instance").get("instance_id")
        for item in _sequence(replay.get("selected_instances"), "selected_instances")
    ]
    identities = _mapping(row.get("final_input_identities"), "final_input_identities")
    _exact_fields(identities, {"seed", "corpus", "runner"}, "final_input_identities")
    if discovery_capture_manifest_path is None:
        raise FurySelectionAdmissionV2Error(
            "validating final admission requires the retained physical "
            "discovery_capture_manifest_path"
        )
    rebuilt = build_final_corpus_admission_receipt(
        candidate_seal=_mapping(row.get("candidate_seal"), "candidate_seal"),
        trusted_external_anchor_seal_sha256s=trusted_external_anchor_seal_sha256s,
        discovery_query=_mapping(row.get("discovery_query"), "discovery_query"),
        discovery_capture_manifest_path=discovery_capture_manifest_path,
        development_exclusion_snapshot=_mapping(
            row.get("development_exclusion_snapshot"),
            "development_exclusion_snapshot",
        ),
        selected_instance_ids=selected_instance_ids,
        final_seed_identity=_mapping(identities.get("seed"), "final seed identity"),
        final_corpus_identity=_mapping(identities.get("corpus"), "final corpus identity"),
        final_runner_identity=_mapping(identities.get("runner"), "final runner identity"),
    )
    if dict(row) != rebuilt:
        raise FurySelectionAdmissionV2Error(
            "final admission is inconsistent, incomplete, or not content-addressed"
        )
    return rebuilt
