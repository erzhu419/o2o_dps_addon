"""Load one explicit Cat2 SavedVariables profile without executing Lua.

This is a deliberately narrow provenance adapter for the current Fury profile.
It accepts only the literal SavedVariables grammar already used by
``import_savedvariables`` and pins every Cat2 source file needed to interpret
the seven post-Brain cards.  The resulting document is a snapshot of mutable
local source, not a sealed release and not permission to execute a policy.

Windows example::

    py -m o2o_dps.cat2_saved_profile_v1 ^
      --savedvariables "D:\\WOW\\WTF\\...\\SavedVariables\\Cat2.lua" ^
      --profile-name "BrainOfCat Shadow" ^
      --installed-root "D:\\WOW\\Interface\\AddOns\\Cat2" ^
      --brainofcat-root "D:\\WOW\\Interface\\AddOns\\BrainOfCat" ^
      --output cat2_profile_snapshot.json
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping, Sequence

from .import_savedvariables import (
    SavedVariablesImportError,
    _LuaTable,
    _Parser,
)


ARTIFACT_TYPE = "cat2_saved_profile_v1"
ARTIFACT_SCHEMA_VERSION = 1
CAT2_SCHEMA_VERSION = 1
AUTHORITY_STATE = "CURRENT_UNSEALED_SOURCE_PROFILE"

EXPECTED_CARD_ORDER = (
    "warrior_o2o_policy_brain",
    "common_auto_attack",
    "warrior_berserker_stance",
    "warrior_bloodrage",
    "warrior_execute",
    "warrior_bloodthirst",
    "warrior_whirlwind",
    "warrior_heroic_strike_alt",
)

# These are byte-for-byte pins of the installed Cat2 files directly used to
# load, order, and interpret the current BrainOfCat Shadow profile.  This is a
# direct dependency boundary, not a claim that every transitive WoW/addon
# helper is sealed.  Updating either addon must cause a closed failure until
# the changed source is reviewed and these pins are advanced.
PINNED_SOURCE_SHA256: dict[str, str] = {
    "Cat2.toc": (
        "69e0dbc613bcc603d9c29eb4b1a575dbcb1c04fe24c5e9b4717744b0724d48c3"
    ),
    "Cat2.lua": (
        "305ef55b802ec45c18ef030150fd63502af5d43b65a59be8862ba87448b371e4"
    ),
    "Core/ConfigurationRunner.lua": (
        "40e8bdcf525a6a7422d07c24d91b3549a5a6667b1307ed8d55e9fcfc0d9ab236"
    ),
    "Core/Persistence.lua": (
        "df9db000b4c10251c4de58a1151e6cc5e82feb976fa844d4ae721d214dac4a87"
    ),
    "Core/CardRegistry.lua": (
        "51bab9b7b2f8fd7b1d7f8b654f9976f4ceea9abafb18a3f25dcf3a4f887e99a6"
    ),
    "Core/CatEvent.lua": (
        "f45d0d9eab7424266d14262cf7c98eb5b1320de231666e7ac15077a996bef81b"
    ),
    "Core/CatLib.lua": (
        "39196bce46c63fb964a215ef1648b5b0fe7660a023b0f5d9b0c520f31db98611"
    ),
    "Core/PlayerInformation.lua": (
        "f3b1dcb5466d8c654e6f112429a75ca93fb358ba94a83408f4869dec8ca3b832"
    ),
    "Cards/Common/AutoAttack.lua": (
        "6e6247ff826e3e8b58c74300609cd3fedd1f2f3ce6a439b235c179f68fac5a1d"
    ),
    "Cards/Warrior/BerserkerStance.lua": (
        "86aaf6d97dc725267cdab2cf242a52299fd4981573c68ee4dfa8911ccd4b8e38"
    ),
    "Cards/Warrior/Bloodrage.lua": (
        "6e7a47e22c50eb46f589f848ee70569bbd7138b8a4da5f6598fee2da2198bdfa"
    ),
    "Cards/Warrior/Execute.lua": (
        "101a0ad7966d9951544fb93b0ec2121163d1139acd59c8edbe9739db69efb3f8"
    ),
    "Cards/Warrior/Bloodthirst.lua": (
        "29d6fd3fde15cfbf5c9229f71fc7514b5333d71e72dad33f2508a12e22c21371"
    ),
    "Cards/Warrior/Whirlwind.lua": (
        "25bbee1ed99f6dbdcfc2b9c4d40826ae09b49532b5d5118d940cf243f5b13591"
    ),
    "Cards/Warrior/HeroicStrikeAlt.lua": (
        "004917192507e78b2313e3524383e46614de466ac4597eac2734b126c3a42095"
    ),
}

PINNED_BRAINOF_CAT_SOURCE_SHA256: dict[str, str] = {
    "addon/O2OPolicyBrainCard.lua": (
        "bdac7adba56d6ce3684c8d58e8da6de74d4849b1c83a428a494de94f8bd0d50a"
    ),
}


class Cat2SavedProfileError(ValueError):
    """The requested Cat2 profile cannot be proven from the supplied inputs."""


def _fail(path: str, message: str) -> Cat2SavedProfileError:
    return Cat2SavedProfileError(f"{path}: {message}")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _utc_mtime(mtime_ns: int) -> str:
    return (
        datetime.fromtimestamp(mtime_ns / 1_000_000_000, timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _stable_read(path: Path, label: str) -> tuple[bytes, os.stat_result]:
    try:
        resolved = path.expanduser().resolve(strict=True)
        if not resolved.is_file():
            raise _fail(label, f"not a regular file: {resolved}")
        before = resolved.stat()
        payload = resolved.read_bytes()
        after = resolved.stat()
    except Cat2SavedProfileError:
        raise
    except OSError as error:
        raise _fail(label, f"cannot read {path}: {error}") from error
    signature_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    signature_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if signature_before != signature_after or len(payload) != after.st_size:
        raise _fail(label, "file changed while it was being read")
    return payload, after


def _table(value: Any, path: str) -> _LuaTable:
    if not isinstance(value, _LuaTable):
        raise _fail(path, f"expected table, got {type(value).__name__}")
    return value


def _named_table(value: Any, path: str) -> dict[str, Any]:
    table = _table(value, path)
    bad_keys = [key for key in table.fields if not isinstance(key, str)]
    if bad_keys:
        raise _fail(path, f"expected only named fields, found {bad_keys!r}")
    return dict(table.fields)


def _dense_array(value: Any, path: str) -> list[Any]:
    table = _table(value, path)
    keys = sorted(table.fields, key=lambda key: (type(key).__name__, repr(key)))
    if any(type(key) is not int or key < 1 for key in keys):
        raise _fail(path, "array keys must be positive integers")
    expected = list(range(1, len(keys) + 1))
    if keys != expected:
        raise _fail(path, f"array must be dense from 1, got keys {keys!r}")
    return [table.fields[index] for index in expected]


def _dense_integer_map(value: Any, path: str) -> dict[int, Any]:
    table = _table(value, path)
    keys = sorted(table.fields, key=lambda key: (type(key).__name__, repr(key)))
    if any(type(key) is not int or key < 1 for key in keys):
        raise _fail(path, "map keys must be positive integers")
    expected = list(range(1, len(keys) + 1))
    if keys != expected:
        raise _fail(path, f"map must be dense from 1, got keys {keys!r}")
    return {index: table.fields[index] for index in expected}


def _require_keys(
    record: Mapping[str, Any],
    path: str,
    *,
    required: set[str],
    allowed: set[str] | None = None,
) -> None:
    missing = sorted(required - set(record))
    if missing:
        raise _fail(path, f"missing required fields {missing!r}")
    if allowed is not None:
        unknown = sorted(set(record) - allowed)
        if unknown:
            raise _fail(path, f"unknown fields {unknown!r}")


def _exact_int(value: Any, path: str) -> int:
    if type(value) is not int:
        raise _fail(path, f"expected integer, got {type(value).__name__}")
    return value


def _nonempty_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise _fail(path, "expected non-empty string")
    return value


def _canonical_number(value: Any, path: str) -> int | float:
    if type(value) not in {int, float}:
        raise _fail(path, f"expected number, got {type(value).__name__}")
    if not math.isfinite(value):
        raise _fail(path, "expected finite number")
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _validate_option_values(
    value: Any,
    *,
    card_id: str,
    path: str,
) -> dict[str, Any]:
    options = _named_table(value, path)
    expected_keys: set[str]
    if card_id == "warrior_o2o_policy_brain":
        expected_keys = {"liveMode"}
        if set(options) != expected_keys:
            raise _fail(path, "Brain options must contain only liveMode")
        if type(options["liveMode"]) is not bool:
            raise _fail(f"{path}.liveMode", "expected boolean")
        if options["liveMode"] is not False:
            raise _fail(
                f"{path}.liveMode",
                "live policy is forbidden; expected false Shadow mode",
            )
        return {"liveMode": False}
    if card_id == "warrior_bloodrage":
        expected_keys = {"maximumRage"}
        if set(options) != expected_keys:
            raise _fail(path, "Bloodrage options must contain only maximumRage")
        maximum = _canonical_number(options["maximumRage"], f"{path}.maximumRage")
        if not 1 <= maximum <= 100:
            raise _fail(f"{path}.maximumRage", "expected value in [1, 100]")
        return {"maximumRage": maximum}
    if card_id == "warrior_heroic_strike_alt":
        expected_keys = {"rageThreshold"}
        if set(options) != expected_keys:
            raise _fail(path, "HeroicStrikeAlt options must contain only rageThreshold")
        threshold = _canonical_number(options["rageThreshold"], f"{path}.rageThreshold")
        if not 1 <= threshold <= 100:
            raise _fail(f"{path}.rageThreshold", "expected value in [1, 100]")
        return {"rageThreshold": threshold}
    if options:
        raise _fail(path, f"card {card_id!r} does not support saved options")
    return {}


def _validate_step(value: Any, path: str) -> dict[str, Any]:
    step = _named_table(value, path)
    _require_keys(
        step,
        path,
        required={"id", "enabled", "optionValues"},
        allowed={"id", "enabled", "optionValues", "minimizedVisible"},
    )
    card_id = _nonempty_string(step["id"], f"{path}.id")
    enabled = _exact_int(step["enabled"], f"{path}.enabled")
    if enabled not in {0, 1}:
        raise _fail(f"{path}.enabled", "expected integer 0 or 1")
    options = _validate_option_values(
        step["optionValues"], card_id=card_id, path=f"{path}.optionValues"
    )
    return {"id": card_id, "enabled": enabled, "option_values": options}


def _validate_repository(
    root: Mapping[str, Any], profile_name: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    _require_keys(
        root,
        "Cat2CharacterDB",
        required={"schemaVersion", "configurations"},
        allowed={"schemaVersion", "configurations", "ui"},
    )
    root_schema = _exact_int(root["schemaVersion"], "Cat2CharacterDB.schemaVersion")
    if root_schema != CAT2_SCHEMA_VERSION:
        raise _fail("Cat2CharacterDB.schemaVersion", "expected schema 1")

    configurations = _named_table(
        root["configurations"], "Cat2CharacterDB.configurations"
    )
    _require_keys(
        configurations,
        "Cat2CharacterDB.configurations",
        required={
            "schemaVersion",
            "activeProfileId",
            "nextProfileId",
            "profileOrder",
            "profiles",
        },
        allowed={
            "schemaVersion",
            "activeProfileId",
            "nextProfileId",
            "profileOrder",
            "profiles",
        },
    )
    repository_schema = _exact_int(
        configurations["schemaVersion"],
        "Cat2CharacterDB.configurations.schemaVersion",
    )
    if repository_schema != CAT2_SCHEMA_VERSION:
        raise _fail("Cat2CharacterDB.configurations.schemaVersion", "expected schema 1")

    active_id = _exact_int(
        configurations["activeProfileId"],
        "Cat2CharacterDB.configurations.activeProfileId",
    )
    next_id = _exact_int(
        configurations["nextProfileId"],
        "Cat2CharacterDB.configurations.nextProfileId",
    )
    order_values = _dense_array(
        configurations["profileOrder"],
        "Cat2CharacterDB.configurations.profileOrder",
    )
    profile_order = [
        _exact_int(value, f"Cat2CharacterDB.configurations.profileOrder[{index}]")
        for index, value in enumerate(order_values, start=1)
    ]
    if not profile_order:
        raise _fail("Cat2CharacterDB.configurations.profileOrder", "must not be empty")
    if len(profile_order) != len(set(profile_order)):
        raise _fail("Cat2CharacterDB.configurations.profileOrder", "profile IDs must be unique")

    profiles = _dense_integer_map(
        configurations["profiles"], "Cat2CharacterDB.configurations.profiles"
    )
    if set(profile_order) != set(profiles):
        raise _fail(
            "Cat2CharacterDB.configurations",
            "profileOrder must contain every dense profiles key exactly once",
        )
    if active_id not in profiles:
        raise _fail(
            "Cat2CharacterDB.configurations.activeProfileId",
            "active profile is absent from profiles",
        )
    if next_id <= max(profiles):
        raise _fail(
            "Cat2CharacterDB.configurations.nextProfileId",
            "must be greater than every profile ID",
        )

    normalized_profiles: dict[int, dict[str, Any]] = {}
    seen_names: set[str] = set()
    for profile_id, raw_profile in profiles.items():
        profile_path = f"Cat2CharacterDB.configurations.profiles[{profile_id}]"
        profile = _named_table(raw_profile, profile_path)
        _require_keys(
            profile,
            profile_path,
            required={"id", "name", "steps"},
            allowed={"id", "name", "steps"},
        )
        declared_id = _exact_int(profile["id"], f"{profile_path}.id")
        if declared_id != profile_id:
            raise _fail(f"{profile_path}.id", "must equal its profiles table key")
        name = _nonempty_string(profile["name"], f"{profile_path}.name")
        if name in seen_names:
            raise _fail(f"{profile_path}.name", f"duplicate profile name {name!r}")
        seen_names.add(name)
        raw_steps = _dense_array(profile["steps"], f"{profile_path}.steps")
        normalized_profiles[profile_id] = {
            "id": profile_id,
            "name": name,
            "raw_steps": raw_steps,
            "path": profile_path,
        }

    selected = [item for item in normalized_profiles.values() if item["name"] == profile_name]
    if len(selected) != 1:
        raise _fail(
            "profile_name",
            f"expected exactly one profile named {profile_name!r}, found {len(selected)}",
        )
    selected_profile = selected[0]
    if selected_profile["id"] != active_id:
        raise _fail(
            "profile_name",
            f"requested profile {profile_name!r} is not the active profile",
        )

    steps = [
        _validate_step(raw_step, f"{selected_profile['path']}.steps[{index}]")
        for index, raw_step in enumerate(selected_profile["raw_steps"], start=1)
    ]
    observed_order = tuple(step["id"] for step in steps)
    if observed_order != EXPECTED_CARD_ORDER:
        unknown = sorted(set(observed_order) - set(EXPECTED_CARD_ORDER))
        suffix = f"; unknown cards {unknown!r}" if unknown else ""
        raise _fail(
            f"{selected_profile['path']}.steps",
            f"expected exact supported order {EXPECTED_CARD_ORDER!r}, got {observed_order!r}{suffix}",
        )
    if any(step["enabled"] != 1 for step in steps):
        raise _fail(
            f"{selected_profile['path']}.steps",
            "all eight supported cards must be enabled",
        )
    if steps[0]["id"] != "warrior_o2o_policy_brain":
        raise _fail(f"{selected_profile['path']}.steps[1]", "Brain card must be first")
    # _validate_option_values already proves this is boolean false; retain an
    # explicit invariant at the profile boundary so future refactors fail closed.
    if steps[0]["option_values"].get("liveMode") is not False:
        raise _fail(f"{selected_profile['path']}.steps[1]", "Brain liveMode must be false")

    semantic_profile = {
        "cat2_schema_version": root_schema,
        "repository_schema_version": repository_schema,
        "profile": {
            "id": selected_profile["id"],
            "name": selected_profile["name"],
            "steps": [
                {
                    "position": index,
                    "id": step["id"],
                    "enabled": step["enabled"],
                    "option_values": step["option_values"],
                }
                for index, step in enumerate(steps, start=1)
            ],
        },
    }
    repository_receipt = {
        "active_profile_id": active_id,
        "next_profile_id": next_id,
        "profile_order": profile_order,
        "profile_count": len(profiles),
    }
    return semantic_profile, repository_receipt


def _resolve_source_root(path: Path, label: str) -> Path:
    try:
        root = path.expanduser().resolve(strict=True)
    except OSError as error:
        raise _fail(label, f"cannot resolve {path}: {error}") from error
    if not root.is_dir():
        raise _fail(label, f"not a directory: {root}")
    return root


def _source_receipt(
    installed_root: Path, brainofcat_root: Path
) -> tuple[dict[str, Any], str]:
    cat2_root = _resolve_source_root(installed_root, "installed_root")
    brain_root = _resolve_source_root(brainofcat_root, "brainofcat_root")

    files: list[dict[str, Any]] = []
    content_identity: list[dict[str, str]] = []
    source_groups = (
        ("cat2", cat2_root, PINNED_SOURCE_SHA256),
        ("brainofcat", brain_root, PINNED_BRAINOF_CAT_SOURCE_SHA256),
    )
    for root_kind, root, pins in source_groups:
        for relative in sorted(pins):
            expected_hash = pins[relative]
            source = (root / Path(relative)).resolve()
            try:
                source.relative_to(root)
            except ValueError as error:
                raise _fail(
                    f"{root_kind}_root", f"source escapes declared root: {relative}"
                ) from error
            payload, stat = _stable_read(source, f"{root_kind}_root/{relative}")
            observed_hash = _sha256(payload)
            if observed_hash != expected_hash:
                raise _fail(
                    f"{root_kind}_root/{relative}",
                    f"source hash mismatch: expected {expected_hash}, got {observed_hash}",
                )
            files.append(
                {
                    "root_kind": root_kind,
                    "relative_path": relative,
                    "size_bytes": len(payload),
                    "mtime_ns": stat.st_mtime_ns,
                    "mtime_utc": _utc_mtime(stat.st_mtime_ns),
                    "sha256": observed_hash,
                }
            )
            content_identity.append(
                {
                    "root_kind": root_kind,
                    "relative_path": relative,
                    "sha256": observed_hash,
                }
            )

    bundle_hash = _sha256(_canonical_bytes(content_identity))
    return (
        {
            "installed_root": str(cat2_root),
            "brainofcat_root": str(brain_root),
            "roots": {"cat2": str(cat2_root), "brainofcat": str(brain_root)},
            "scope": "DIRECT_RUNTIME_DEPENDENCY_PINNED",
            "transitive_dependency_closure_claimed": False,
            "pin_status": "PINNED_EXACT",
            "file_count": len(files),
            "files": files,
            "sha256": bundle_hash,
        },
        bundle_hash,
    )


def load_cat2_saved_profile(
    savedvariables_path: str | Path,
    profile_name: str,
    installed_root: str | Path,
    brainofcat_root: str | Path,
) -> dict[str, Any]:
    """Return a strict source-bound snapshot for one active Cat2 profile.

    All four inputs are mandatory.  Lua is parsed as data only: no interpreter,
    addon loader, import hook, or process execution is involved.
    """

    if not isinstance(profile_name, str) or not profile_name:
        raise _fail("profile_name", "must be a non-empty explicit string")
    savedvariables = Path(savedvariables_path)
    raw, stat = _stable_read(savedvariables, "savedvariables_path")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError as error:
        raise _fail("savedvariables_path", f"must be UTF-8 text: {error}") from error
    try:
        assignments = _Parser(text, str(savedvariables)).parse()
    except SavedVariablesImportError as error:
        raise _fail(
            "savedvariables_path",
            f"not a literal-only WoW SavedVariables document: {error}",
        ) from error
    if set(assignments) != {"Cat2CharacterDB"}:
        raise _fail(
            "savedvariables_path",
            "the only permitted top-level assignment is Cat2CharacterDB",
        )
    root = _named_table(assignments["Cat2CharacterDB"], "Cat2CharacterDB")
    semantic_profile, repository = _validate_repository(root, profile_name)
    profile_hash = _sha256(_canonical_bytes(semantic_profile))
    sources, source_hash = _source_receipt(
        Path(installed_root), Path(brainofcat_root)
    )
    resolved_savedvariables = savedvariables.expanduser().resolve(strict=True)
    raw_hash = _sha256(raw)

    return {
        "artifact_type": ARTIFACT_TYPE,
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "status": "ok",
        "authority_state": AUTHORITY_STATE,
        "execution_authorized": False,
        "deployment_allowed": False,
        "savedvariables": {
            "path": str(resolved_savedvariables),
            "top_level_assignment": "Cat2CharacterDB",
            "size_bytes": len(raw),
            "mtime_ns": stat.st_mtime_ns,
            "mtime_utc": _utc_mtime(stat.st_mtime_ns),
            "sha256": raw_hash,
        },
        "selection": {
            "requested_profile_name": profile_name,
            **repository,
        },
        "profile": semantic_profile["profile"],
        "profile_semantic_document": semantic_profile,
        "source_bundle": sources,
        "raw_savedvariables_sha256": raw_hash,
        "profile_semantic_sha256": profile_hash,
        "source_bundle_sha256": source_hash,
    }


def build_cat2_saved_profile_snapshot(
    savedvariables_path: str | Path,
    profile_name: str,
    installed_root: str | Path,
    brainofcat_root: str | Path,
) -> dict[str, Any]:
    """Compatibility spelling for callers that treat the result as an artifact."""

    return load_cat2_saved_profile(
        savedvariables_path, profile_name, installed_root, brainofcat_root
    )


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    destination = path.expanduser().resolve()
    # The snapshot itself carries the exact input provenance, so the writer can
    # enforce non-overwrite independently of the CLI argument parser.  Resolve
    # both sides before comparing to cover aliases, ``..``, and symlinks.
    try:
        protected = {
            Path(str(value["savedvariables"]["path"])).expanduser().resolve(),
        }
        source_roots = {
            str(kind): Path(str(root)).expanduser().resolve()
            for kind, root in value["source_bundle"]["roots"].items()
        }
        protected.update(
            (
                source_roots[str(item["root_kind"])]
                / Path(str(item["relative_path"]))
            ).resolve()
            for item in value["source_bundle"]["files"]
        )
    except (KeyError, TypeError, ValueError) as error:
        raise _fail("output", "snapshot provenance is incomplete") from error
    if destination in protected:
        raise _fail(
            "output",
            f"refusing to overwrite a SavedVariables or pinned source input: {destination}",
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    ) + "\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, destination)
    except (OSError, UnicodeError, ValueError) as error:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise _fail("output", f"cannot atomically write {destination}: {error}") from error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--savedvariables", type=Path, required=True)
    parser.add_argument("--profile-name", required=True)
    parser.add_argument("--installed-root", type=Path, required=True)
    parser.add_argument("--brainofcat-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    document = load_cat2_saved_profile(
        args.savedvariables,
        args.profile_name,
        args.installed_root,
        args.brainofcat_root,
    )
    _atomic_write_json(args.output, document)
    print(
        json.dumps(
            {
                "status": "ok",
                "output": str(args.output.expanduser().resolve()),
                "profile_name": args.profile_name,
                "profile_semantic_sha256": document["profile_semantic_sha256"],
                "source_bundle_sha256": document["source_bundle_sha256"],
                "authority_state": AUTHORITY_STATE,
            },
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
