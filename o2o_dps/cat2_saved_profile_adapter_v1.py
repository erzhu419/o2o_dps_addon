"""Non-executing adapter for a validated, current Cat2 saved profile snapshot.

The adapter preserves Cat2's enabled step order and stop-on-true behavior over
``FuryExpertState``.  It is source-derived evidence for a deployed profile,
not execution of Lua.  The current profile is explicitly unsealed, so its
decisions are never eligible for an independent expert vote.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping

from .cat2_saved_profile_v1 import (
    ARTIFACT_SCHEMA_VERSION,
    ARTIFACT_TYPE,
    AUTHORITY_STATE,
    EXPECTED_CARD_ORDER,
    PINNED_BRAINOF_CAT_SOURCE_SHA256,
    PINNED_SOURCE_SHA256,
)
from .expert_policy import (
    ExpertDecision,
    ExpertProvenance,
    ExpertRole,
    ProvenanceKind,
    RawSink,
    StanceOp,
    SwingQueueOp,
)
from .fury_expert_adapters import (
    BLOODRAGE,
    BLOODTHIRST,
    EXECUTE,
    WHIRLWIND,
    FuryExpertState,
    _DecisionBuilder,
)


SOURCE_BUNDLE_SCOPE = "DIRECT_RUNTIME_DEPENDENCY_PINNED"
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")


class Cat2SavedProfileAdapterError(ValueError):
    """A snapshot is not the strict artifact emitted by the v1 loader."""


def _fail(path: str, message: str) -> Cat2SavedProfileAdapterError:
    return Cat2SavedProfileAdapterError(f"{path}: {message}")


def _dict(value: Any, path: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise _fail(path, f"expected JSON object, got {type(value).__name__}")
    if any(not isinstance(key, str) for key in value):
        raise _fail(path, "all object keys must be strings")
    return dict(value)


def _list(value: Any, path: str) -> list[Any]:
    if type(value) is not list:
        raise _fail(path, f"expected JSON array, got {type(value).__name__}")
    return list(value)


def _keys(record: Mapping[str, Any], expected: set[str], path: str) -> None:
    if set(record) != expected:
        raise _fail(
            path,
            f"expected exact fields {sorted(expected)!r}, got {sorted(record)!r}",
        )


def _exact_int(value: Any, path: str) -> int:
    if type(value) is not int:
        raise _fail(path, f"expected integer, got {type(value).__name__}")
    return value


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise _fail(path, "expected non-empty string")
    return value


def _digest(value: Any, path: str) -> str:
    digest = _string(value, path)
    if _SHA256.fullmatch(digest) is None:
        raise _fail(path, "expected lowercase SHA-256")
    return digest


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
        raise _fail("snapshot", f"not strict JSON data: {error}") from error


def _canonical_number(value: Any, path: str) -> int | float:
    if type(value) not in {int, float} or not math.isfinite(value):
        raise _fail(path, "expected finite number")
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


@dataclass(frozen=True)
class _Step:
    position: int
    card_id: str
    options: Mapping[str, Any]


@dataclass(frozen=True)
class _ValidatedSnapshot:
    profile_id: int
    profile_name: str
    steps: tuple[_Step, ...]
    savedvariables_path: str
    authority_files: tuple[str, ...]
    raw_sha256: str
    semantic_sha256: str
    source_sha256: str


def _validate_options(card_id: str, value: Any, path: str) -> dict[str, Any]:
    options = _dict(value, path)
    if card_id == "warrior_o2o_policy_brain":
        _keys(options, {"liveMode"}, path)
        if options["liveMode"] is not False:
            raise _fail(f"{path}.liveMode", "must be boolean false")
        return {"liveMode": False}
    if card_id == "warrior_bloodrage":
        _keys(options, {"maximumRage"}, path)
        maximum = _canonical_number(options["maximumRage"], f"{path}.maximumRage")
        if not 1 <= maximum <= 100:
            raise _fail(f"{path}.maximumRage", "expected value in [1, 100]")
        return {"maximumRage": maximum}
    if card_id == "warrior_heroic_strike_alt":
        _keys(options, {"rageThreshold"}, path)
        threshold = _canonical_number(
            options["rageThreshold"], f"{path}.rageThreshold"
        )
        if not 1 <= threshold <= 100:
            raise _fail(f"{path}.rageThreshold", "expected value in [1, 100]")
        return {"rageThreshold": threshold}
    _keys(options, set(), path)
    return {}


def _validate_snapshot(snapshot: Mapping[str, Any]) -> _ValidatedSnapshot:
    if type(snapshot) is not dict:
        raise _fail("snapshot", "must be the JSON object returned by the v1 loader")
    if snapshot.get("artifact_type") != ARTIFACT_TYPE:
        raise _fail("snapshot.artifact_type", f"expected {ARTIFACT_TYPE!r}")
    if snapshot.get("schema_version") != ARTIFACT_SCHEMA_VERSION or type(
        snapshot.get("schema_version")
    ) is not int:
        raise _fail("snapshot.schema_version", "expected integer 1")
    if snapshot.get("status") != "ok":
        raise _fail("snapshot.status", "expected 'ok'")
    if snapshot.get("authority_state") != AUTHORITY_STATE:
        raise _fail("snapshot.authority_state", f"expected {AUTHORITY_STATE!r}")
    if snapshot.get("execution_authorized") is not False:
        raise _fail("snapshot.execution_authorized", "must be false")
    if snapshot.get("deployment_allowed") is not False:
        raise _fail("snapshot.deployment_allowed", "must be false")

    raw_sha = _digest(
        snapshot.get("raw_savedvariables_sha256"),
        "snapshot.raw_savedvariables_sha256",
    )
    semantic_sha = _digest(
        snapshot.get("profile_semantic_sha256"),
        "snapshot.profile_semantic_sha256",
    )
    source_sha = _digest(
        snapshot.get("source_bundle_sha256"),
        "snapshot.source_bundle_sha256",
    )

    savedvariables = _dict(snapshot.get("savedvariables"), "snapshot.savedvariables")
    savedvariables_path = _string(
        savedvariables.get("path"), "snapshot.savedvariables.path"
    )
    if savedvariables.get("top_level_assignment") != "Cat2CharacterDB":
        raise _fail(
            "snapshot.savedvariables.top_level_assignment",
            "expected Cat2CharacterDB",
        )
    if savedvariables.get("sha256") != raw_sha:
        raise _fail("snapshot.savedvariables.sha256", "does not match raw snapshot hash")

    profile = _dict(snapshot.get("profile"), "snapshot.profile")
    _keys(profile, {"id", "name", "steps"}, "snapshot.profile")
    profile_id = _exact_int(profile["id"], "snapshot.profile.id")
    if profile_id < 1:
        raise _fail("snapshot.profile.id", "must be positive")
    profile_name = _string(profile["name"], "snapshot.profile.name")
    raw_steps = _list(profile["steps"], "snapshot.profile.steps")
    if len(raw_steps) != len(EXPECTED_CARD_ORDER):
        raise _fail("snapshot.profile.steps", "expected exactly eight steps")

    steps: list[_Step] = []
    for position, (raw_step, expected_card) in enumerate(
        zip(raw_steps, EXPECTED_CARD_ORDER), start=1
    ):
        path = f"snapshot.profile.steps[{position}]"
        step = _dict(raw_step, path)
        _keys(step, {"position", "id", "enabled", "option_values"}, path)
        if _exact_int(step["position"], f"{path}.position") != position:
            raise _fail(f"{path}.position", "step positions must be dense and ordered")
        card_id = _string(step["id"], f"{path}.id")
        if card_id != expected_card:
            raise _fail(
                f"{path}.id", f"expected {expected_card!r}, got {card_id!r}"
            )
        if _exact_int(step["enabled"], f"{path}.enabled") != 1:
            raise _fail(f"{path}.enabled", "all source profile steps must be enabled")
        options = _validate_options(card_id, step["option_values"], f"{path}.option_values")
        steps.append(_Step(position, card_id, options))

    semantic_document = _dict(
        snapshot.get("profile_semantic_document"),
        "snapshot.profile_semantic_document",
    )
    expected_semantic = {
        "cat2_schema_version": 1,
        "repository_schema_version": 1,
        "profile": profile,
    }
    if semantic_document != expected_semantic:
        raise _fail(
            "snapshot.profile_semantic_document",
            "does not exactly match the normalized selected profile",
        )
    observed_semantic_sha = hashlib.sha256(
        _canonical_bytes(expected_semantic)
    ).hexdigest()
    if observed_semantic_sha != semantic_sha:
        raise _fail(
            "snapshot.profile_semantic_sha256",
            f"content hash mismatch: got {semantic_sha}, computed {observed_semantic_sha}",
        )

    selection = _dict(snapshot.get("selection"), "snapshot.selection")
    if selection.get("requested_profile_name") != profile_name:
        raise _fail("snapshot.selection.requested_profile_name", "profile mismatch")
    if selection.get("active_profile_id") != profile_id or type(
        selection.get("active_profile_id")
    ) is not int:
        raise _fail("snapshot.selection.active_profile_id", "profile is not active")
    profile_order = _list(
        selection.get("profile_order"), "snapshot.selection.profile_order"
    )
    if any(type(item) is not int for item in profile_order) or profile_id not in profile_order:
        raise _fail("snapshot.selection.profile_order", "active profile is absent")

    bundle = _dict(snapshot.get("source_bundle"), "snapshot.source_bundle")
    if bundle.get("scope") != SOURCE_BUNDLE_SCOPE:
        raise _fail("snapshot.source_bundle.scope", f"expected {SOURCE_BUNDLE_SCOPE!r}")
    if bundle.get("transitive_dependency_closure_claimed") is not False:
        raise _fail(
            "snapshot.source_bundle.transitive_dependency_closure_claimed",
            "must be false",
        )
    if bundle.get("pin_status") != "PINNED_EXACT":
        raise _fail("snapshot.source_bundle.pin_status", "expected PINNED_EXACT")
    if bundle.get("sha256") != source_sha:
        raise _fail("snapshot.source_bundle.sha256", "does not match source bundle hash")
    roots = _dict(bundle.get("roots"), "snapshot.source_bundle.roots")
    _keys(roots, {"cat2", "brainofcat"}, "snapshot.source_bundle.roots")
    for kind in ("cat2", "brainofcat"):
        _string(roots[kind], f"snapshot.source_bundle.roots.{kind}")

    expected_sources = [
        ("cat2", relative, digest)
        for relative, digest in sorted(PINNED_SOURCE_SHA256.items())
    ] + [
        ("brainofcat", relative, digest)
        for relative, digest in sorted(PINNED_BRAINOF_CAT_SOURCE_SHA256.items())
    ]
    source_files = _list(bundle.get("files"), "snapshot.source_bundle.files")
    if bundle.get("file_count") != len(source_files) or type(
        bundle.get("file_count")
    ) is not int:
        raise _fail("snapshot.source_bundle.file_count", "does not match files")
    if len(source_files) != len(expected_sources):
        raise _fail("snapshot.source_bundle.files", "unexpected dependency count")

    content_identity: list[dict[str, str]] = []
    authority_files = [savedvariables_path]
    for index, (raw_source, expected) in enumerate(
        zip(source_files, expected_sources), start=1
    ):
        path = f"snapshot.source_bundle.files[{index}]"
        source = _dict(raw_source, path)
        root_kind = source.get("root_kind")
        relative = source.get("relative_path")
        digest = source.get("sha256")
        if (root_kind, relative, digest) != expected:
            raise _fail(path, f"expected pinned identity {expected!r}")
        content_identity.append(
            {
                "root_kind": root_kind,
                "relative_path": relative,
                "sha256": digest,
            }
        )
        authority_files.append(str(Path(roots[root_kind]) / Path(relative)))
    observed_source_sha = hashlib.sha256(_canonical_bytes(content_identity)).hexdigest()
    if observed_source_sha != source_sha:
        raise _fail(
            "snapshot.source_bundle_sha256",
            f"content hash mismatch: got {source_sha}, computed {observed_source_sha}",
        )

    return _ValidatedSnapshot(
        profile_id=profile_id,
        profile_name=profile_name,
        steps=tuple(steps),
        savedvariables_path=savedvariables_path,
        authority_files=tuple(authority_files),
        raw_sha256=raw_sha,
        semantic_sha256=semantic_sha,
        source_sha256=source_sha,
    )


class Cat2SavedProfileSourceAdapterV1:
    """Translate the exact validated active Cat2 profile without executing it."""

    expert_id = "cat2.fury.brainofcat_shadow.saved_profile_source_v1"

    def __init__(self, snapshot: Mapping[str, Any]) -> None:
        self._snapshot = _validate_snapshot(snapshot)

    def _provenance(self) -> ExpertProvenance:
        return ExpertProvenance(
            expert_id=self.expert_id,
            kind=ProvenanceKind.SOURCE_DERIVED,
            role=ExpertRole.DEPLOYED,
            authority_files=self._snapshot.authority_files,
            source_refs=(
                "addon/O2OPolicyBrainCard.lua:49-81",
                "Core/ConfigurationRunner.lua:260-389",
                "Cards/Common/AutoAttack.lua:30-54",
                "Cards/Warrior/BerserkerStance.lua:29-47",
                "Cards/Warrior/Bloodrage.lua:35-51",
                "Cards/Warrior/Execute.lua:40-48",
                "Cards/Warrior/Bloodthirst.lua:24-36",
                "Cards/Warrior/Whirlwind.lua:35-62",
                "Cards/Warrior/HeroicStrikeAlt.lua:35-57",
            ),
        )

    def _base_metadata(self) -> dict[str, Any]:
        return {
            "profile_id": self._snapshot.profile_id,
            "profile_name": self._snapshot.profile_name,
            "profile_step_order": [step.card_id for step in self._snapshot.steps],
            "authority_state": AUTHORITY_STATE,
            "source_bundle_scope": SOURCE_BUNDLE_SCOPE,
            "transitive_dependency_closure_claimed": False,
            "source_execution": False,
            "current_profile_unsealed": True,
            "independent_vote_blocker": AUTHORITY_STATE,
            "raw_savedvariables_sha256": self._snapshot.raw_sha256,
            "profile_semantic_sha256": self._snapshot.semantic_sha256,
            "source_bundle_sha256": self._snapshot.source_sha256,
            # Cat2's Whirlwind card intentionally submits Cat2.Cast while
            # SpellReadyOffset is strictly below 0.5 seconds.  The WoW spell
            # API is a state-preserving no-op until the cooldown is actually
            # legal, after which the next macro invocation retries.  This
            # narrow declaration is consumed by the simulator closed loop; it
            # does not authorize a retry for any other action, operation, or
            # illegality reason.
            "source_api_noop_retry_contracts": [
                {
                    "lane": "gcd",
                    "action": WHIRLWIND,
                    "operation": "Cat2.Cast",
                    "minimum_ready_in_ms_inclusive": 1,
                    "maximum_ready_in_ms_exclusive": 500,
                    "retry_wait_ms": 100,
                    "source_ref": "Cards/Warrior/Whirlwind.lua:35-62",
                    "reason": "SpellReadyOffset<0.5 failed-cast retry window",
                }
            ],
        }

    def propose(self, state: FuryExpertState) -> ExpertDecision:
        if not isinstance(state, FuryExpertState):
            raise TypeError("state must be FuryExpertState")
        builder = _DecisionBuilder()
        trace: list[dict[str, Any]] = []
        for step in self._snapshot.steps:
            stopped, effect = self._apply_step(builder, state, step)
            trace.append(
                {
                    "position": step.position,
                    "id": step.card_id,
                    "effect": effect,
                    "stopped": stopped,
                }
            )
            if stopped:
                break

        metadata = self._base_metadata()
        metadata["evaluated_step_order"] = [item["id"] for item in trace]
        metadata["step_trace"] = trace
        metadata["stopped_by_card"] = trace[-1]["id"] if trace[-1]["stopped"] else None
        return builder.build(
            self._provenance(),
            role_can_vote=False,
            reason=(
                "source-derived replay of the current deployed Cat2 profile; "
                "non-voting because the mutable source profile is unsealed"
            ),
            metadata=metadata,
        )

    def _apply_step(
        self,
        builder: _DecisionBuilder,
        state: FuryExpertState,
        step: _Step,
    ) -> tuple[bool, str]:
        card_id = step.card_id
        if card_id == "warrior_o2o_policy_brain":
            # The validated liveMode=false option makes the card record a
            # proposal and return false; it has no action sink in Shadow mode.
            return False, "shadow_brain_pass_through"

        if card_id == "common_auto_attack":
            builder.raw.append(
                RawSink(
                    "autoattack",
                    "Cat2.StartAttack",
                    "START",
                    "Cards/Common/AutoAttack.lua:38-54",
                )
            )
            return False, "raw_autoattack_side_effect"

        if card_id == "warrior_berserker_stance":
            if state.current_stance is not StanceOp.BERSERKER:
                builder.emit_stance(
                    StanceOp.BERSERKER,
                    operation="CastShapeshiftForm/Cat2.Cast",
                    value="狂暴姿态",
                    source_ref="Cards/Warrior/BerserkerStance.lua:29-47",
                )
                return True, "stance_sink_stop"
            return False, "already_in_berserker_stance"

        if card_id == "warrior_bloodrage":
            maximum = float(step.options["maximumRage"])
            if (
                state.in_combat
                and state.target_exists
                and state.in_melee_range
                and state.target_distance_yards <= 5.0
                and state.rage < maximum
                and state.bloodrage_ready
            ):
                builder.emit_off_gcd(
                    BLOODRAGE,
                    operation="Cat2.Cast",
                    value="血性狂暴",
                    source_ref="Cards/Warrior/Bloodrage.lua:35-51",
                )
                return False, "bloodrage_sink_continue"
            return False, "bloodrage_condition_false"

        if card_id == "warrior_execute":
            if (
                state.target_exists
                and state.rage >= state.execute_cost
                and state.target_health_pct < 19.9
            ):
                builder.emit_gcd(
                    EXECUTE,
                    operation="Cat2.Cast",
                    value="斩杀",
                    source_ref="Cards/Warrior/Execute.lua:40-48",
                )
                return True, "execute_sink_stop"
            return False, "execute_condition_false"

        if card_id == "warrior_bloodthirst":
            if (
                state.target_exists
                and state.bloodthirst_known
                and state.rage >= 30.0
                and state.cooldown_ready(state.bloodthirst_ready_in_s)
            ):
                builder.emit_gcd(
                    BLOODTHIRST,
                    operation="Cat2.Cast",
                    value="嗜血",
                    source_ref="Cards/Warrior/Bloodthirst.lua:24-36",
                )
                return True, "bloodthirst_sink_stop"
            return False, "bloodthirst_condition_false"

        if card_id == "warrior_whirlwind":
            if (
                state.target_exists
                and state.current_stance is StanceOp.BERSERKER
                and state.target_distance_yards <= 7.0
                and state.rage >= state.whirlwind_cost
                and state.whirlwind_ready_in_s < 0.5
            ):
                builder.emit_gcd(
                    WHIRLWIND,
                    operation="Cat2.Cast",
                    value="旋风斩",
                    source_ref="Cards/Warrior/Whirlwind.lua:35-62",
                )
                return True, "whirlwind_sink_stop"
            return False, "whirlwind_condition_false"

        if card_id == "warrior_heroic_strike_alt":
            threshold = float(step.options["rageThreshold"])
            if state.target_exists and state.rage >= threshold:
                queue = (
                    SwingQueueOp.CLEAVE
                    if state.nearby_enemies >= 2
                    else SwingQueueOp.HEROIC_STRIKE
                )
                builder.emit_queue(
                    queue,
                    operation="Cat2.Cast",
                    value="顺劈斩" if queue is SwingQueueOp.CLEAVE else "英勇打击",
                    source_ref="Cards/Warrior/HeroicStrikeAlt.lua:35-57",
                )
                return False, "next_swing_sink_continue"
            return False, "heroic_strike_alt_condition_false"

        # Constructor validation makes this unreachable; keep the source loop
        # closed if a future refactor changes the allowed profile shape.
        raise Cat2SavedProfileAdapterError(f"unsupported profile card {card_id!r}")


# Explicit descriptive alias for callers that do not encode the artifact
# version in class names.  The legacy Cat2ProfileAdapter is intentionally
# untouched and retains its existing defaults.
Cat2DeployedSavedProfileSourceAdapter = Cat2SavedProfileSourceAdapterV1
