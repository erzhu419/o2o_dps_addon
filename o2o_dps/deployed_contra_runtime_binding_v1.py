"""Bind deployed Contra source identity to current Buttons and Nampower CVars.

The existing runtime snapshot stores both ``ContraDB.Warrior.Buttons`` and a
legacy adapter projection.  The deployed Lua does not use the lowercase
``baofa``/``shengcun`` fields as its outer gates: it reads the distinct
``Burst``/``Survive`` keys.  This helper therefore derives executable values
from the exact keys read by ``Contra_ALL.lua`` and records any disagreement
with the legacy projection instead of silently inheriting it.

This is a source-derived simulator binding.  It does not prove that the DLL
loaded, that the public NameAndGuild gate passed, or that the client accepted
any sink.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

from .deployed_contra_source_manifest_v1 import (
    DEFAULT_ARCHIVE,
    DEFAULT_SOURCE_ROOT,
    DeployedContraSourceManifestError,
    SCHEMA as SOURCE_MANIFEST_SCHEMA,
    SOURCE_POLICY_ID,
    build_deployed_contra_source_manifest_v1,
    verify_deployed_contra_archive_equivalence_v1,
)
from .fury_expert_runtime_snapshot_v1 import (
    SCHEMA as RUNTIME_SNAPSHOT_SCHEMA,
    sha256_json,
)


JSONMap = dict[str, Any]
SCHEMA = "deployed_contra_runtime_binding/v1"

_REQUIRED_BUTTON_TYPES: dict[str, type] = {
    "fangan": str,
    "mode": str,
    "autoselect": bool,
    "xuanfeng": bool,
    "baofa": bool,
    "shengcun": bool,
    "silie": bool,
    "quanbudaduan": bool,
    "zhidingdaduan": bool,
    "liunudaduan": bool,
    "bossothuanwuqi": bool,
    "xiaoguaiothuanwuqi": bool,
}
_QUEUE_CVAR_CONTRACT = {
    "NP_QueueChannelingSpells": "0",
    "NP_QueueTargetingSpells": "0",
    "NP_QueueOnSwingSpells": "1",
    "NP_QueueSpellsOnCooldown": "0",
    "NP_RetryServerRejectedSpells": "0",
}


class DeployedContraRuntimeBindingError(RuntimeError):
    """The source manifest or current-character snapshot cannot be bound."""


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DeployedContraRuntimeBindingError(f"{label} must be an object")
    return value


def _validate_content_hash(
    document: Mapping[str, Any], hash_field: str, label: str
) -> str:
    observed = document.get(hash_field)
    if not isinstance(observed, str) or len(observed) != 64:
        raise DeployedContraRuntimeBindingError(
            f"{label}.{hash_field} must be a SHA-256"
        )
    unhashed = dict(document)
    unhashed.pop(hash_field, None)
    expected = sha256_json(unhashed)
    if observed != expected:
        raise DeployedContraRuntimeBindingError(
            f"{label}.{hash_field} mismatch"
        )
    return observed


def _validate_source_manifest(manifest: Mapping[str, Any]) -> tuple[str, str]:
    if manifest.get("schema") != SOURCE_MANIFEST_SCHEMA:
        raise DeployedContraRuntimeBindingError(
            f"source manifest schema must equal {SOURCE_MANIFEST_SCHEMA}"
        )
    if manifest.get("source_policy_id") != SOURCE_POLICY_ID:
        raise DeployedContraRuntimeBindingError("source policy identity mismatch")
    manifest_sha = _validate_content_hash(
        manifest, "manifest_sha256", "source manifest"
    )
    boundary = _mapping(
        manifest.get("authority_boundary"), "source manifest authority boundary"
    )
    if boundary.get("source_identity_verified") is not True:
        raise DeployedContraRuntimeBindingError(
            "source manifest has not verified source identity"
        )
    for field in (
        "public_macro_entry_verified",
        "client_load_observed",
        "client_execution_observed",
        "client_acceptance_observed",
        "server_outcome_observed",
        "comparison_ready",
    ):
        if boundary.get(field) is not False:
            raise DeployedContraRuntimeBindingError(
                f"source manifest overclaims {field}"
            )
    identity = _mapping(manifest.get("identity"), "source manifest identity")
    closure = _mapping(identity.get("loaded_closure"), "loaded closure")
    closure_sha = closure.get("sha256")
    if not isinstance(closure_sha, str) or len(closure_sha) != 64:
        raise DeployedContraRuntimeBindingError(
            "loaded closure sha256 is malformed"
        )
    return manifest_sha, closure_sha


def _validate_snapshot(snapshot: Mapping[str, Any]) -> str:
    if snapshot.get("schema") != RUNTIME_SNAPSHOT_SCHEMA:
        raise DeployedContraRuntimeBindingError(
            f"runtime snapshot schema must equal {RUNTIME_SNAPSHOT_SCHEMA}"
        )
    snapshot_sha = _validate_content_hash(
        snapshot, "snapshot_sha256", "runtime snapshot"
    )
    authority = _mapping(snapshot.get("authority"), "runtime snapshot authority")
    if authority.get("comparison_eligible") is not False:
        raise DeployedContraRuntimeBindingError(
            "runtime snapshot must not claim comparison eligibility"
        )
    context = _mapping(snapshot.get("character_context"), "runtime character_context")
    if context.get("status") != "BOUND_SAME_CHARACTER_DIRECTORY_AND_BUILD_CAPTURE":
        raise DeployedContraRuntimeBindingError(
            "runtime snapshot lacks same-character configuration evidence"
        )
    for field in ("character_context_id", "build_capture_semantic_sha256"):
        value = context.get(field)
        if not isinstance(value, str) or len(value) != 64:
            raise DeployedContraRuntimeBindingError(
                f"runtime character_context.{field} must be a SHA-256"
            )
    if not isinstance(snapshot.get("fixed_character_build"), Mapping):
        raise DeployedContraRuntimeBindingError(
            "runtime snapshot lacks the fixed character build"
        )
    return snapshot_sha


def _effective_buttons(snapshot: Mapping[str, Any]) -> tuple[JSONMap, JSONMap]:
    profile = _mapping(
        snapshot.get("contra_current_profile"), "contra_current_profile"
    )
    buttons = _mapping(profile.get("buttons"), "contra_current_profile.buttons")
    for key, expected_type in _REQUIRED_BUTTON_TYPES.items():
        value = buttons.get(key)
        if not isinstance(value, expected_type):
            raise DeployedContraRuntimeBindingError(
                f"Contra Buttons.{key} must be {expected_type.__name__}"
            )
    if buttons["mode"] != "副本模式":
        raise DeployedContraRuntimeBindingError(
            "deployed Fury raid binding requires Buttons.mode=副本模式"
        )

    # Lua reads these exact case-sensitive keys.  Missing is nil and therefore
    # disables the outer helper (`if not Buttons.Burst then return end`).
    executable = {
        "mode": buttons["mode"],
        "selected_scheme_label": buttons["fangan"],
        "autoselect": buttons["autoselect"],
        "xuanfeng": buttons["xuanfeng"],
        "burst": buttons.get("Burst") is True,
        "survival": buttons.get("Survive") is True,
        "interrupt_enabled": buttons.get("interrupt") is True,
        "rend": buttons["silie"],
        "dual_wield_boss_weapon_swap": buttons["bossothuanwuqi"],
        "dual_wield_nonboss_weapon_swap": buttons["xiaoguaiothuanwuqi"],
        "legacy_lowercase_baofa": buttons["baofa"],
        "legacy_lowercase_shengcun": buttons["shengcun"],
    }
    legacy = _mapping(
        profile.get("adapter_core_projection"),
        "contra_current_profile.adapter_core_projection",
    )
    disagreements = {
        field: {"legacy_projection": legacy.get(field), "source_exact": value}
        for field, value in (
            ("burst", executable["burst"]),
            ("survival", executable["survival"]),
            ("interrupt_enabled", executable["interrupt_enabled"]),
        )
        if legacy.get(field) != value
    }
    diagnostics = {
        "effective_table": "ContraDB.Warrior.Buttons",
        "selected_saved_scheme_is_not_substituted_for_buttons": True,
        "case_sensitive_runtime_gate_keys": {
            "burst": "Burst",
            "survival": "Survive",
            "interrupt_enabled": "interrupt",
        },
        "legacy_projection_disagreements": disagreements,
        "buttons_semantic_sha256": profile.get("buttons_semantic_sha256"),
        "selected_saved_scheme_sha256": profile.get(
            "selected_saved_scheme_sha256"
        ),
    }
    return executable, diagnostics


def _cvar_binding(snapshot: Mapping[str, Any]) -> JSONMap:
    cvars = _mapping(snapshot.get("nampower_cvars"), "nampower_cvars")
    observed = {name: cvars.get(name) for name in _QUEUE_CVAR_CONTRACT}
    if any(value not in {"0", "1"} for value in observed.values()):
        raise DeployedContraRuntimeBindingError(
            "Nampower queue CVars must be captured as binary string values"
        )
    inputs = _mapping(snapshot.get("inputs"), "runtime snapshot inputs")
    nampower = _mapping(inputs.get("nampower_dll"), "inputs.nampower_dll")
    digest = nampower.get("sha256")
    size = nampower.get("size_bytes")
    if not isinstance(digest, str) or len(digest) != 64:
        raise DeployedContraRuntimeBindingError("nampower.dll identity is malformed")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise DeployedContraRuntimeBindingError("nampower.dll size is invalid")
    return {
        "observed_config_values": observed,
        "source_initialization_values": dict(_QUEUE_CVAR_CONTRACT),
        "matches_source_initialization": observed == _QUEUE_CVAR_CONTRACT,
        "config_semantic_sha256": snapshot.get(
            "nampower_cvars_semantic_sha256"
        ),
        "nampower_dll": {"sha256": digest, "size_bytes": size},
        "dll_present_in_captured_environment": True,
        "dll_loaded_in_client_observed": False,
        "queue_on_swing_enabled": observed["NP_QueueOnSwingSpells"] == "1",
        "queue_spells_on_cooldown_enabled": (
            observed["NP_QueueSpellsOnCooldown"] == "1"
        ),
        "retry_server_rejected_spells_enabled": (
            observed["NP_RetryServerRejectedSpells"] == "1"
        ),
    }


def build_deployed_contra_runtime_binding_v1(
    *,
    source_manifest: Mapping[str, Any],
    runtime_snapshot: Mapping[str, Any],
) -> JSONMap:
    """Build the bounded v7 input object without changing a frozen runner."""

    manifest_sha, closure_sha = _validate_source_manifest(source_manifest)
    snapshot_sha = _validate_snapshot(runtime_snapshot)
    executable, diagnostics = _effective_buttons(runtime_snapshot)
    cvars = _cvar_binding(runtime_snapshot)
    character_context = _mapping(
        runtime_snapshot.get("character_context"), "runtime character_context"
    )
    contra_input = _mapping(
        _mapping(runtime_snapshot.get("inputs"), "runtime snapshot inputs").get(
            "contra_savedvariables"
        ),
        "inputs.contra_savedvariables",
    )
    contra_saved_sha = contra_input.get("sha256")
    if not isinstance(contra_saved_sha, str) or len(contra_saved_sha) != 64:
        raise DeployedContraRuntimeBindingError(
            "Contra SavedVariables identity is malformed"
        )

    document: JSONMap = {
        "schema": SCHEMA,
        "source_policy_id": SOURCE_POLICY_ID,
        "source_manifest_sha256": manifest_sha,
        "loaded_source_closure_sha256": closure_sha,
        "runtime_snapshot_sha256": snapshot_sha,
        "character_context_id": character_context["character_context_id"],
        "fixed_character_build_sha256": sha256_json(
            runtime_snapshot["fixed_character_build"]
        ),
        "contra_savedvariables_sha256": contra_saved_sha,
        "runtime_profile": executable,
        "runtime_profile_diagnostics": diagnostics,
        "source_refs": {
            "interrupt_runtime_gate": "Contra_ALL.lua:31390-31392",
            "burst_runtime_gate": "Contra_ALL.lua:31476-31479",
            "survival_runtime_gate": "Contra_ALL.lua:31524-31526",
            "warrior_nampower_cvars": "Contra_ALL.lua:36512-36518",
            "public_macro_access_gate": "Contra_ALL.lua:36384,36435",
        },
        "nampower": cvars,
        "adapter_inputs": {
            "saved_mode": executable["mode"],
            "saved_xuanfeng": executable["xuanfeng"],
            "saved_baofa_legacy": executable["legacy_lowercase_baofa"],
            "burst_runtime_gate": executable["burst"],
            "survival_runtime_gate": executable["survival"],
            "interrupt_runtime_gate": executable["interrupt_enabled"],
            "autoselect": executable["autoselect"],
            "queue_on_swing": cvars["queue_on_swing_enabled"],
            "queue_spells_on_cooldown": cvars[
                "queue_spells_on_cooldown_enabled"
            ],
            "retry_server_rejected_spells": cvars[
                "retry_server_rejected_spells_enabled"
            ],
        },
        "authority_boundary": {
            "loaded_source_identity_bound": True,
            "current_buttons_consumed": True,
            "captured_config_cvars_consumed": True,
            "same_character_configuration_bound": True,
            "configuration_complete_for_source_derived_simulator": cvars[
                "matches_source_initialization"
            ],
            "same_character_runtime_parity_proven": False,
            "public_macro_entry_verified": False,
            "client_execution_observed": False,
            "comparison_ready": False,
        },
        "v7_integration_points": [
            "replace ContraDeployedSourceAdapter class constants with adapter_inputs",
            "bind this identity into the paired-runner baseline cache key",
            "add distinct raid_a and raid_b full-controller registrations",
            "retain policyEntered and ordered client acceptance as promotion gates",
        ],
    }
    document["binding_sha256"] = sha256_json(document)
    return document


def validate_deployed_contra_runtime_binding_v1(
    value: Mapping[str, Any],
) -> JSONMap:
    """Validate a serialized binding without re-reading third-party files."""

    try:
        raw = json.loads(
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise DeployedContraRuntimeBindingError(
            f"runtime binding is not strict JSON: {error}"
        ) from error
    if not isinstance(raw, dict):
        raise DeployedContraRuntimeBindingError(
            "runtime binding must be an object"
        )
    if raw.get("schema") != SCHEMA:
        raise DeployedContraRuntimeBindingError(
            f"runtime binding schema must equal {SCHEMA}"
        )
    if raw.get("source_policy_id") != SOURCE_POLICY_ID:
        raise DeployedContraRuntimeBindingError(
            "runtime binding source policy identity mismatch"
        )
    _validate_content_hash(raw, "binding_sha256", "runtime binding")
    for field in (
        "source_manifest_sha256",
        "loaded_source_closure_sha256",
        "runtime_snapshot_sha256",
        "contra_savedvariables_sha256",
        "character_context_id",
        "fixed_character_build_sha256",
    ):
        digest = raw.get(field)
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise DeployedContraRuntimeBindingError(
                f"runtime binding {field} must be a lowercase SHA-256"
            )

    profile = _mapping(raw.get("runtime_profile"), "runtime_profile")
    adapter = _mapping(raw.get("adapter_inputs"), "adapter_inputs")
    for key in (
        "autoselect",
        "xuanfeng",
        "burst",
        "survival",
        "interrupt_enabled",
        "rend",
        "dual_wield_boss_weapon_swap",
        "dual_wield_nonboss_weapon_swap",
        "legacy_lowercase_baofa",
        "legacy_lowercase_shengcun",
    ):
        if not isinstance(profile.get(key), bool):
            raise DeployedContraRuntimeBindingError(
                f"runtime_profile.{key} must be boolean"
            )
    if profile.get("mode") != "副本模式":
        raise DeployedContraRuntimeBindingError(
            "runtime binding requires the deployed Fury raid mode"
        )
    expected_adapter = {
        "saved_mode": profile["mode"],
        "saved_xuanfeng": profile["xuanfeng"],
        "saved_baofa_legacy": profile["legacy_lowercase_baofa"],
        "burst_runtime_gate": profile["burst"],
        "survival_runtime_gate": profile["survival"],
        "interrupt_runtime_gate": profile["interrupt_enabled"],
        "autoselect": profile["autoselect"],
    }
    if any(adapter.get(key) != item for key, item in expected_adapter.items()):
        raise DeployedContraRuntimeBindingError(
            "adapter inputs disagree with the bound runtime profile"
        )

    nampower = _mapping(raw.get("nampower"), "nampower")
    observed = _mapping(
        nampower.get("observed_config_values"),
        "nampower.observed_config_values",
    )
    if set(observed) != set(_QUEUE_CVAR_CONTRACT) or any(
        value not in {"0", "1"} for value in observed.values()
    ):
        raise DeployedContraRuntimeBindingError(
            "runtime binding Nampower CVar values are malformed"
        )
    expected_cvar_inputs = {
        "queue_on_swing": observed["NP_QueueOnSwingSpells"] == "1",
        "queue_spells_on_cooldown": (
            observed["NP_QueueSpellsOnCooldown"] == "1"
        ),
        "retry_server_rejected_spells": (
            observed["NP_RetryServerRejectedSpells"] == "1"
        ),
    }
    if any(adapter.get(key) is not item for key, item in expected_cvar_inputs.items()):
        raise DeployedContraRuntimeBindingError(
            "adapter inputs disagree with the bound Nampower CVars"
        )
    if nampower.get("source_initialization_values") != _QUEUE_CVAR_CONTRACT or (
        nampower.get("matches_source_initialization")
        is not (dict(observed) == _QUEUE_CVAR_CONTRACT)
    ):
        raise DeployedContraRuntimeBindingError(
            "Nampower source-initialization comparison is inconsistent"
        )

    authority = _mapping(raw.get("authority_boundary"), "authority_boundary")
    expected_complete = nampower["matches_source_initialization"]
    for field, expected in (
        ("loaded_source_identity_bound", True),
        ("current_buttons_consumed", True),
        ("captured_config_cvars_consumed", True),
        ("same_character_configuration_bound", True),
        ("configuration_complete_for_source_derived_simulator", expected_complete),
        ("same_character_runtime_parity_proven", False),
        ("public_macro_entry_verified", False),
        ("client_execution_observed", False),
        ("comparison_ready", False),
    ):
        if authority.get(field) is not expected:
            raise DeployedContraRuntimeBindingError(
                f"runtime binding authority boundary disagrees on {field}"
            )
    return raw


def load_runtime_snapshot(path: str | Path) -> JSONMap:
    resolved = Path(path).expanduser().resolve()
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DeployedContraRuntimeBindingError(
            f"could not read runtime snapshot {resolved}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise DeployedContraRuntimeBindingError(
            "runtime snapshot root must be an object"
        )
    return value


def load_deployed_contra_runtime_binding_v1(path: str | Path) -> JSONMap:
    """Load and validate one serialized deployed-Contra runtime binding."""

    resolved = Path(path).expanduser().resolve()
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DeployedContraRuntimeBindingError(
            f"could not read runtime binding {resolved}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise DeployedContraRuntimeBindingError(
            "runtime binding root must be an object"
        )
    return validate_deployed_contra_runtime_binding_v1(value)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-snapshot", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest = build_deployed_contra_source_manifest_v1(args.source_root)
        verify_deployed_contra_archive_equivalence_v1(manifest, args.archive)
        snapshot = load_runtime_snapshot(args.runtime_snapshot)
        result = build_deployed_contra_runtime_binding_v1(
            source_manifest=manifest,
            runtime_snapshot=snapshot,
        )
    except (
        DeployedContraRuntimeBindingError,
        DeployedContraSourceManifestError,
        OSError,
    ) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DeployedContraRuntimeBindingError",
    "SCHEMA",
    "build_deployed_contra_runtime_binding_v1",
    "load_deployed_contra_runtime_binding_v1",
    "load_runtime_snapshot",
    "validate_deployed_contra_runtime_binding_v1",
]
