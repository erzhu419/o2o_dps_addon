"""Adjudicate the Cat2 Whirlwind no-op proxy in the frozen full replay.

The supplemental full replay labeled every bounded, early ``Cat2.Cast``
attempt as ``source_api_noop_unknown_blocker_proxy`` because the bridge does
not return a structured rejection code.  This sidecar does not rewrite that
completed artifact.  It pins the full artifact, receipt, 66-file input lock,
and request bundle, then replays only the current Cat2 adapter on the same 392
families and 16 seeds while retaining event-level blocker evidence in compact
counters.

The adjudication is deliberately narrow: a positive Whirlwind action cooldown
is a sufficient blocker, even without claiming that every possible blocker is
enumerated.  The fixed 100 ms retry cadence remains an explicit non-faithful
timing proxy.  All voting, deployment, and real-game claims remain disabled.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from math import floor
from pathlib import Path
import tempfile
from typing import Any, Mapping, Sequence

from .cat2_saved_profile_adapter_v1 import Cat2SavedProfileSourceAdapterV1
from .fury_current_cat2_heldout_replay_v1 import (
    CAT2_ID,
    FuryCurrentCat2HeldoutReplayError,
    InputFileIdentity,
    verify_run_input_snapshot,
)
from .fury_current_cat2_short_horizon_sensitivity_v1 import (
    DEFAULT_FULL_ARTIFACT,
    DEFAULT_FULL_ARTIFACT_SHA256,
    DEFAULT_FULL_RECEIPT,
    DEFAULT_FULL_RECEIPT_SHA256,
    DEFAULT_INPUT_LOCK,
    DEFAULT_INPUT_LOCK_SHA256,
    DEFAULT_REQUEST_BUNDLE_SHA256,
    DEFAULT_RUN_INPUT_FILE_BUNDLE_SHA256,
    UpstreamEvidence,
    reconstruct_locked_inputs,
    verify_upstream_evidence,
)
from .fury_expert_closed_loop import (
    ClosedLoopBridgeLike,
    run_fury_expert_closed_loop,
)
from .fury_heldout_corpus_gate_v1 import SelectedCorpus
from .sim_bridge import SimulatorBridge


JSONMap = dict[str, Any]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent

DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "offline_data"
    / "sim_validation"
    / "fury_current_cat2_noop_adjudication_v1.json"
)

OLD_REASON = "gcd:source_api_noop_unknown_blocker_proxy"
MECHANISM_CLASSIFICATION = "source_api_noop_spell_cooldown_sufficient_proxy"
TIMING_PROXY_REASON = "source_api_noop_fixed_100ms_retry_timing_proxy"
WHIRLWIND_ACTION_KEY = "warrior.whirlwind"
WHIRLWIND_SPELL_ID = 1680
EXPECTED_CAT2_ROLLOUT_COUNT = 392 * 16
EXPECTED_PROXY_EVENT_COUNT = 19_429
NOMINAL_RETRY_WAIT_MS = 100

MECHANISM_SOURCE_PATHS = (
    WORKSPACE_ROOT / "Cat2" / "Core" / "CatLib.lua",
    WORKSPACE_ROOT / "Cat2" / "Cards" / "Warrior" / "Whirlwind.lua",
    WORKSPACE_ROOT / "wowsims-turtle" / "sim" / "o2o" / "environment.go",
    WORKSPACE_ROOT / "wowsims-turtle" / "sim" / "core" / "spell.go",
    WORKSPACE_ROOT / "wowsims-turtle" / "sim" / "warrior" / "whirlwind.go",
)


class NoopAdjudicationError(RuntimeError):
    """The pinned evidence or narrow cooldown adjudication failed closed."""


@dataclass
class NoopEvidenceAccumulator:
    event_count: int = 0
    rollout_count: int = 0
    rollouts_with_event: int = 0
    ready_in_ms: Counter[int] = field(default_factory=Counter)
    gcd_remaining_ms: Counter[int] = field(default_factory=Counter)
    commanded_wait_ms: Counter[int] = field(default_factory=Counter)
    events_by_target_stratum: Counter[str] = field(default_factory=Counter)
    events_per_rollout: Counter[int] = field(default_factory=Counter)
    evidence_true_counts: Counter[str] = field(default_factory=Counter)
    violations: Counter[str] = field(default_factory=Counter)

    def record_rollout(self, event_count: int) -> None:
        self.rollout_count += 1
        self.events_per_rollout[event_count] += 1
        if event_count:
            self.rollouts_with_event += 1

    def to_dict(self) -> JSONMap:
        return {
            "event_count": self.event_count,
            "rollout_count": self.rollout_count,
            "rollouts_with_event": self.rollouts_with_event,
            "rollouts_without_event": self.rollout_count - self.rollouts_with_event,
            "ready_in_ms_distribution": _counter_dict(self.ready_in_ms),
            "gcd_remaining_ms_distribution": _counter_dict(self.gcd_remaining_ms),
            "commanded_wait_ms_distribution": _counter_dict(
                self.commanded_wait_ms
            ),
            "events_by_target_count_stratum": dict(
                sorted(self.events_by_target_stratum.items())
            ),
            "events_per_rollout_distribution": _counter_dict(
                self.events_per_rollout
            ),
            "evidence_true_counts": dict(sorted(self.evidence_true_counts.items())),
            "violation_count": sum(self.violations.values()),
            "violation_counts": dict(sorted(self.violations.items())),
        }


def _counter_dict(counter: Counter[int]) -> JSONMap:
    return {str(key): counter[key] for key in sorted(counter)}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _artifact_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _snapshot(path: Path, role: str) -> InputFileIdentity:
    resolved = path.expanduser().resolve()
    try:
        raw = resolved.read_bytes()
    except OSError as exc:
        raise NoopAdjudicationError(f"could not read {role} {resolved}: {exc}") from exc
    return InputFileIdentity(
        role=role,
        path=str(resolved),
        size_bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def capture_sidecar_sources() -> tuple[InputFileIdentity, ...]:
    """Address sidecar code and static mechanism evidence outside the 66-file lock."""

    import o2o_dps.fury_current_cat2_short_horizon_sensitivity_v1 as upstream_module

    values = [
        _snapshot(Path(__file__), "noop_adjudication_python_source"),
        _snapshot(
            Path(upstream_module.__file__),
            "noop_adjudication_upstream_helper_source",
        ),
    ]
    values.extend(
        _snapshot(path, "static_mechanism_source")
        for path in MECHANISM_SOURCE_PATHS
    )
    return tuple(values)


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise NoopAdjudicationError(f"{label} must be an object")
    return value


def expected_full_proxy_count(
    evidence: UpstreamEvidence,
    *,
    expected_proxy_count: int = EXPECTED_PROXY_EVENT_COUNT,
) -> int:
    if (
        isinstance(expected_proxy_count, bool)
        or not isinstance(expected_proxy_count, int)
        or expected_proxy_count < 0
    ):
        raise NoopAdjudicationError("expected full proxy count must be a nonnegative integer")
    evaluation = _require_mapping(
        evidence.full_artifact.get("evaluation"), "full.evaluation"
    )
    overall = _require_mapping(evaluation.get("overall"), "full.evaluation.overall")
    ranking = overall.get("ranking")
    if not isinstance(ranking, list):
        raise NoopAdjudicationError("full ranking must be an array")
    rows = [
        row
        for row in ranking
        if isinstance(row, Mapping) and row.get("expert_id") == CAT2_ID
    ]
    if len(rows) != 1:
        raise NoopAdjudicationError("full ranking must contain one current Cat2 row")
    reasons = _require_mapping(
        rows[0].get("nonfaithful_reason_counts"),
        "full Cat2 nonfaithful_reason_counts",
    )
    value = reasons.get(OLD_REASON)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise NoopAdjudicationError("full Cat2 proxy count is invalid")
    if value != expected_proxy_count:
        raise NoopAdjudicationError(
            f"pinned full proxy count changed: expected {expected_proxy_count}, got {value}"
        )
    return value


def _record_check(
    accumulator: NoopEvidenceAccumulator,
    name: str,
    condition: bool,
) -> None:
    if condition:
        accumulator.evidence_true_counts[name] += 1
    else:
        accumulator.violations[name] += 1


def adjudicate_step(
    step: Mapping[str, Any],
    *,
    target_count_stratum: str,
    accumulator: NoopEvidenceAccumulator,
) -> int:
    """Record compact evidence for every old no-op command in one transition."""

    commands = step.get("commands")
    if not isinstance(commands, list):
        accumulator.violations["commands_not_array"] += 1
        return 0
    old_reasons = step.get("nonfaithful_reasons")
    old_reason_count = (
        sum(value == OLD_REASON for value in old_reasons)
        if isinstance(old_reasons, list)
        else -1
    )
    proxy_commands = [
        row
        for row in commands
        if isinstance(row, Mapping)
        and row.get("operation") == "source_api_noop_wait"
    ]
    if old_reason_count != len(proxy_commands):
        accumulator.violations["old_reason_command_count_mismatch"] += abs(
            old_reason_count - len(proxy_commands)
        ) or 1

    for command in proxy_commands:
        accumulator.event_count += 1
        accumulator.events_by_target_stratum[str(target_count_stratum)] += 1
        before = _require_mapping(
            step.get("simulator_state_before"), "step.simulator_state_before"
        )
        expert = _require_mapping(step.get("expert_state"), "step.expert_state")
        proposal = _require_mapping(step.get("proposal"), "step.proposal")
        available = _require_mapping(command.get("available"), "command.available")
        action = _require_mapping(command.get("action"), "command.action")
        result = _require_mapping(command.get("result"), "command.result")
        power = _require_mapping(before.get("power"), "state.power")

        ready = available.get("ready_in_ms")
        gcd = before.get("gcd_remaining_ms")
        wait_ms = result.get("wait_ms")
        raw_rage = power.get("current")
        expert_rage = expert.get("rage")
        whirlwind_cost = expert.get("whirlwind_cost")
        distance = expert.get("target_distance_yards")

        if isinstance(ready, bool) or not isinstance(ready, int):
            accumulator.violations["ready_in_ms_not_int"] += 1
            continue
        if isinstance(gcd, bool) or not isinstance(gcd, int):
            accumulator.violations["gcd_remaining_ms_not_int"] += 1
            continue
        if isinstance(wait_ms, bool) or not isinstance(wait_ms, int):
            accumulator.violations["commanded_wait_ms_not_int"] += 1
            continue
        accumulator.ready_in_ms[ready] += 1
        accumulator.gcd_remaining_ms[gcd] += 1
        accumulator.commanded_wait_ms[wait_ms] += 1

        source_attempts = command.get("source_attempts")
        source_attempt_ok = (
            isinstance(source_attempts, list)
            and len(source_attempts) == 1
            and isinstance(source_attempts[0], Mapping)
            and source_attempts[0].get("channel") == "gcd"
            and source_attempts[0].get("operation") == "Cat2.Cast"
        )
        proposal_gcd = proposal.get("gcd")
        proposal_is_whirlwind = (
            isinstance(proposal_gcd, Mapping)
            and proposal_gcd.get("action") == WHIRLWIND_ACTION_KEY
        )
        contract_rows = _require_mapping(
            proposal.get("metadata"), "proposal.metadata"
        ).get("source_api_noop_retry_contracts")
        contract_ok = False
        if isinstance(contract_rows, list) and len(contract_rows) == 1:
            contract = contract_rows[0]
            contract_ok = (
                isinstance(contract, Mapping)
                and contract.get("lane") == "gcd"
                and contract.get("action") == WHIRLWIND_ACTION_KEY
                and contract.get("operation") == "Cat2.Cast"
                and contract.get("minimum_ready_in_ms_inclusive") == 1
                and contract.get("maximum_ready_in_ms_exclusive") == 500
                and contract.get("retry_wait_ms") == NOMINAL_RETRY_WAIT_MS
            )

        numeric_rage_ok = (
            isinstance(raw_rage, (int, float))
            and not isinstance(raw_rage, bool)
            and isinstance(expert_rage, (int, float))
            and not isinstance(expert_rage, bool)
            and isinstance(whirlwind_cost, (int, float))
            and not isinstance(whirlwind_cost, bool)
        )
        rage_ok = bool(
            numeric_rage_ok
            and floor(float(raw_rage)) == float(expert_rage)
            and float(expert_rage) >= float(whirlwind_cost)
        )
        distance_ok = (
            isinstance(distance, (int, float))
            and not isinstance(distance, bool)
            and float(distance) <= 7.0
        )

        _record_check(accumulator, "action_is_whirlwind_1680", action == {"spell_id": WHIRLWIND_SPELL_ID})
        _record_check(accumulator, "proposal_is_whirlwind", proposal_is_whirlwind)
        _record_check(accumulator, "single_cat2_cast_source_attempt", source_attempt_ok)
        _record_check(accumulator, "bounded_retry_contract_exact", contract_ok)
        _record_check(accumulator, "available_legal_false", available.get("legal") is False)
        _record_check(accumulator, "action_triggers_gcd", available.get("triggers_gcd") is True)
        _record_check(accumulator, "spell_cooldown_positive_below_500ms", 1 <= ready < 500)
        _record_check(accumulator, "gcd_already_ready", gcd == 0)
        _record_check(accumulator, "no_hardcast_present", before.get("current_cast") is None)
        _record_check(accumulator, "bridge_needs_input", before.get("needs_input") is True)
        _record_check(accumulator, "encounter_not_finished", before.get("finished") is False)
        _record_check(accumulator, "rage_meets_whirlwind_cost", rage_ok)
        _record_check(accumulator, "berserker_stance", expert.get("current_stance") == "BERSERKER")
        _record_check(accumulator, "target_exists", expert.get("target_exists") is True)
        _record_check(accumulator, "target_within_7_yards", distance_ok)
        _record_check(accumulator, "nominal_or_horizon_capped_wait", 1 <= wait_ms <= NOMINAL_RETRY_WAIT_MS)
        _record_check(accumulator, "command_status_known_noop", command.get("status") == "known_noop")

    return len(proxy_commands)


def replay_cat2_for_adjudication(
    bridge: ClosedLoopBridgeLike,
    corpus: SelectedCorpus,
    *,
    cat2_snapshot: Mapping[str, Any],
    validation_seeds: Sequence[int],
    progress: Any = None,
) -> NoopEvidenceAccumulator:
    adapter = Cat2SavedProfileSourceAdapterV1(cat2_snapshot)
    accumulator = NoopEvidenceAccumulator()
    for family_ordinal, family in enumerate(corpus.families, start=1):
        for seed in validation_seeds:
            event_count = 0

            def sink(step: JSONMap) -> None:
                nonlocal event_count
                event_count += adjudicate_step(
                    step,
                    target_count_stratum=family.target_count_stratum,
                    accumulator=accumulator,
                )

            rollout = run_fury_expert_closed_loop(
                bridge,
                family.scenario.request,
                adapter,
                seed=seed,
                horizon_ms=family.scenario.horizon_ms,
                transition_sink=sink,
                retain_steps=False,
            )
            reasons = rollout.get("nonfaithful_reason_counts")
            rollout_old_count = (
                reasons.get(OLD_REASON, 0) if isinstance(reasons, Mapping) else None
            )
            if rollout_old_count != event_count:
                accumulator.violations[
                    "rollout_old_reason_transition_count_mismatch"
                ] += 1
            if rollout.get("omitted_lane_count") != 0:
                accumulator.violations["rollout_has_omitted_lane"] += 1
            if rollout.get("configured_horizon_complete") is not True:
                accumulator.violations["rollout_horizon_incomplete"] += 1
            accumulator.record_rollout(event_count)
        if progress is not None:
            progress(
                {
                    "family_ordinal": family_ordinal,
                    "family_count": len(corpus.families),
                    "completed_cat2_rollouts": family_ordinal
                    * len(validation_seeds),
                    "proxy_event_count": accumulator.event_count,
                }
            )
    return accumulator


def _source_bundle_sha256(values: Sequence[InputFileIdentity]) -> str:
    return hashlib.sha256(
        _canonical_bytes([asdict(value) for value in values])
    ).hexdigest()


def build_adjudication_artifact(
    *,
    evidence: UpstreamEvidence,
    accumulator: NoopEvidenceAccumulator,
    validation_seeds: Sequence[int],
    family_count: int,
    sidecar_sources: Sequence[InputFileIdentity],
    expected_proxy_count: int = EXPECTED_PROXY_EVENT_COUNT,
) -> JSONMap:
    expected = expected_full_proxy_count(
        evidence, expected_proxy_count=expected_proxy_count
    )
    if family_count != 392 or len(validation_seeds) != 16:
        raise NoopAdjudicationError("adjudication did not use the 392-family, 16-seed contract")
    if accumulator.rollout_count != EXPECTED_CAT2_ROLLOUT_COUNT:
        raise NoopAdjudicationError(
            f"expected {EXPECTED_CAT2_ROLLOUT_COUNT} Cat2 rollouts, got {accumulator.rollout_count}"
        )
    if accumulator.event_count != expected:
        raise NoopAdjudicationError(
            f"replay proxy count differs from full artifact: {accumulator.event_count} != {expected}"
        )
    if sum(accumulator.violations.values()) != 0:
        raise NoopAdjudicationError(
            f"adjudication evidence contains {sum(accumulator.violations.values())} violations"
        )
    if set(accumulator.ready_in_ms) != {100, 200, 300, 400}:
        raise NoopAdjudicationError(
            "formal replay ready_in_ms support is not exactly {100,200,300,400}"
        )
    if set(accumulator.gcd_remaining_ms) != {0}:
        raise NoopAdjudicationError("formal replay contains a concurrent GCD blocker")

    event_evidence = accumulator.to_dict()
    return {
        "schema_version": 1,
        "kind": "fury_current_cat2_noop_adjudication_v1",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "COMPLETED",
        "upstream_provenance": {
            "completed_full_artifact": asdict(evidence.full_identity),
            "completed_full_receipt": asdict(evidence.receipt_identity),
            "supplemental_input_lock": asdict(evidence.lock_identity),
            "locked_file_count": len(evidence.lock_snapshot.files),
            "run_input_file_bundle_sha256": evidence.lock_snapshot.file_bundle_sha256,
            "request_bundle_sha256": evidence.lock_snapshot.request_contract[
                "request_bundle_sha256"
            ],
            "receipt_artifact_lock_request_chain_verified": True,
            "locked_inputs_reverified_before_and_after_replay": True,
        },
        "sidecar_source_provenance": {
            "files": [asdict(value) for value in sidecar_sources],
            "bundle_sha256": _source_bundle_sha256(sidecar_sources),
            "static_source_to_locked_bridge_binary_build_binding_claimed": False,
            "note": (
                "The locked bridge binary supplies behavioral evidence. Current Cat2/Go "
                "sources are content-addressed static mechanism corroboration, not a "
                "reproducible-build proof for that binary."
            ),
        },
        "replay_contract": {
            "policy": CAT2_ID,
            "family_count": family_count,
            "validation_seeds": list(validation_seeds),
            "validation_seed_count": len(validation_seeds),
            "cat2_rollout_count": accumulator.rollout_count,
            "retained_transition_rows": False,
            "compact_event_counters_only": True,
            "same_locked_reconstructed_requests": True,
        },
        "adjudication": {
            "old_reason": OLD_REASON,
            "old_reason_count_in_full": expected,
            "replayed_old_reason_count": accumulator.event_count,
            "mechanism_classification": MECHANISM_CLASSIFICATION,
            "mechanism_classified_count": accumulator.event_count,
            "unresolved_unknown_blocker_count": 0,
            "cooldown_is_sufficient_not_necessarily_exhaustive": True,
            "concurrent_gcd_observed_count": (
                accumulator.event_count - accumulator.gcd_remaining_ms.get(0, 0)
            ),
            "mechanism_adjudication_supported": True,
            "exact_game_client_rejection_code_observed": False,
            "exact_lua_execution": False,
            "remaining_nonfaithful_reason_counts": {
                TIMING_PROXY_REASON: accumulator.event_count
            },
            "nominal_retry_wait_ms": NOMINAL_RETRY_WAIT_MS,
            "timing_proxy_preserved": True,
        },
        "event_evidence": event_evidence,
        "bridge_field_inventory": {
            "already_available_and_used": [
                "AvailableAction.action",
                "AvailableAction.legal",
                "AvailableAction.ready_in_ms",
                "AvailableAction.triggers_gcd",
                "State.gcd_remaining_ms",
                "State.current_cast",
                "State.needs_input",
                "State.finished",
                "State.power.current",
                "State.auras (stance inference)",
                "request player.distanceFromTarget",
            ],
            "not_available_as_structured_bridge_fields": [
                "legality_blockers or cast rejection reason",
                "separate spell-CD and shared-CD remaining values",
                "current resource cost after modifiers",
                "explicit warrior stance enum",
                "current channel state and remaining time",
                "target range/line-of-sight/facing blocker result",
                "game-client CastSpellByName return or error code",
                "real macro keypress timestamps",
            ],
            "bridge_change_required_for_this_whirlwind_adjudication": False,
            "optional_generalization": (
                "For action-agnostic adjudication, add structured blocker flags and "
                "separate spell/shared/GCD timers; do not replace the pinned full result."
            ),
        },
        "claim_boundary": {
            "simulator_diagnostic_only": True,
            "source_execution": False,
            "exact_lua_replay": False,
            "historical_heldout_gate_modified": False,
            "historical_heldout_gate_passed": False,
            "diagnostic_quality_passed": False,
            "independent_expert_vote_available": False,
            "voting_result": False,
            "deployment_allowed": False,
            "real_game_superiority_claimed": False,
        },
    }


def _atomic_write(path: Path, data: bytes) -> None:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=resolved.parent,
            prefix=f".{resolved.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
        temporary.replace(resolved)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def write_adjudication_with_receipt(
    output: Path,
    artifact: Mapping[str, Any],
    *,
    evidence: UpstreamEvidence,
) -> JSONMap:
    boundary = _require_mapping(artifact.get("claim_boundary"), "claim_boundary")
    for key in (
        "historical_heldout_gate_modified",
        "historical_heldout_gate_passed",
        "diagnostic_quality_passed",
        "independent_expert_vote_available",
        "voting_result",
        "deployment_allowed",
        "real_game_superiority_claimed",
    ):
        if boundary.get(key) is not False:
            raise NoopAdjudicationError(f"claim_boundary.{key} must remain false")
    data = _artifact_bytes(artifact)
    _atomic_write(output, data)
    committed = output.expanduser().resolve().read_bytes()
    if committed != data:
        raise NoopAdjudicationError("committed adjudication bytes differ")
    identity = InputFileIdentity(
        role="cat2_noop_adjudication_artifact",
        path=str(output.expanduser().resolve()),
        size_bytes=len(committed),
        sha256=hashlib.sha256(committed).hexdigest(),
    )
    adjudication = _require_mapping(artifact.get("adjudication"), "adjudication")
    receipt: JSONMap = {
        "schema_version": 1,
        "kind": "fury_current_cat2_noop_adjudication_receipt_v1",
        "artifact": asdict(identity),
        "completed_full_artifact_sha256": evidence.full_identity.sha256,
        "completed_full_receipt_sha256": evidence.receipt_identity.sha256,
        "supplemental_input_lock_sha256": evidence.lock_identity.sha256,
        "run_input_file_bundle_sha256": evidence.lock_snapshot.file_bundle_sha256,
        "request_bundle_sha256": evidence.lock_snapshot.request_contract[
            "request_bundle_sha256"
        ],
        "cat2_rollout_count": artifact["replay_contract"]["cat2_rollout_count"],
        "proxy_event_count": adjudication["replayed_old_reason_count"],
        "mechanism_classification": adjudication["mechanism_classification"],
        "violation_count": artifact["event_evidence"]["violation_count"],
        "sidecar_source_bundle_sha256": artifact["sidecar_source_provenance"][
            "bundle_sha256"
        ],
        "simulator_diagnostic_only": True,
        "voting_result": False,
        "deployment_allowed": False,
        "real_game_superiority_claimed": False,
    }
    receipt_path = Path(str(output.expanduser().resolve()) + ".receipt.json")
    _atomic_write(receipt_path, _artifact_bytes(receipt))
    if json.loads(receipt_path.read_bytes()) != receipt:
        raise NoopAdjudicationError("adjudication receipt verification failed")
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full-artifact", type=Path, default=DEFAULT_FULL_ARTIFACT)
    parser.add_argument("--full-receipt", type=Path, default=DEFAULT_FULL_RECEIPT)
    parser.add_argument("--input-lock", type=Path, default=DEFAULT_INPUT_LOCK)
    parser.add_argument("--expected-full-sha256", default=DEFAULT_FULL_ARTIFACT_SHA256)
    parser.add_argument("--expected-receipt-sha256", default=DEFAULT_FULL_RECEIPT_SHA256)
    parser.add_argument("--expected-input-lock-sha256", default=DEFAULT_INPUT_LOCK_SHA256)
    parser.add_argument(
        "--expected-run-input-file-bundle-sha256",
        default=DEFAULT_RUN_INPUT_FILE_BUNDLE_SHA256,
    )
    parser.add_argument(
        "--expected-request-bundle-sha256", default=DEFAULT_REQUEST_BUNDLE_SHA256
    )
    parser.add_argument(
        "--expected-full-proxy-count",
        type=int,
        default=EXPECTED_PROXY_EVENT_COUNT,
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    evidence = verify_upstream_evidence(
        full_artifact_path=args.full_artifact,
        full_receipt_path=args.full_receipt,
        input_lock_path=args.input_lock,
        expected_full_sha256=args.expected_full_sha256,
        expected_receipt_sha256=args.expected_receipt_sha256,
        expected_lock_sha256=args.expected_input_lock_sha256,
        expected_run_input_file_bundle_sha256=(
            args.expected_run_input_file_bundle_sha256
        ),
        expected_request_bundle_sha256=args.expected_request_bundle_sha256,
    )
    expected_full_proxy_count(
        evidence, expected_proxy_count=args.expected_full_proxy_count
    )
    sidecar_sources = capture_sidecar_sources()
    corpus, contract, _, cat2_snapshot, bridge_path = reconstruct_locked_inputs(
        evidence
    )
    seeds = tuple(contract["validation_seeds"])
    with SimulatorBridge(bridge_path) as bridge:
        accumulator = replay_cat2_for_adjudication(
            bridge,
            corpus,
            cat2_snapshot=cat2_snapshot,
            validation_seeds=seeds,
            progress=lambda value: print(
                json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                flush=True,
            )
            if value["family_ordinal"] % 25 == 0
            else None,
        )
    try:
        verify_run_input_snapshot(evidence.lock_snapshot, corpus)
    except FuryCurrentCat2HeldoutReplayError as exc:
        raise NoopAdjudicationError(str(exc)) from exc
    verify_upstream_evidence(
        full_artifact_path=args.full_artifact,
        full_receipt_path=args.full_receipt,
        input_lock_path=args.input_lock,
        expected_full_sha256=args.expected_full_sha256,
        expected_receipt_sha256=args.expected_receipt_sha256,
        expected_lock_sha256=args.expected_input_lock_sha256,
        expected_run_input_file_bundle_sha256=(
            args.expected_run_input_file_bundle_sha256
        ),
        expected_request_bundle_sha256=args.expected_request_bundle_sha256,
    )
    if capture_sidecar_sources() != sidecar_sources:
        raise NoopAdjudicationError("sidecar or static mechanism sources changed during replay")
    artifact = build_adjudication_artifact(
        evidence=evidence,
        accumulator=accumulator,
        validation_seeds=seeds,
        family_count=len(corpus.families),
        sidecar_sources=sidecar_sources,
        expected_proxy_count=args.expected_full_proxy_count,
    )
    receipt = write_adjudication_with_receipt(
        args.output,
        artifact,
        evidence=evidence,
    )
    print(
        json.dumps(
            {
                "status": "COMPLETED",
                "cat2_rollout_count": accumulator.rollout_count,
                "proxy_event_count": accumulator.event_count,
                "mechanism_classification": MECHANISM_CLASSIFICATION,
                "violation_count": sum(accumulator.violations.values()),
                "output": str(args.output.expanduser().resolve()),
                "output_sha256": receipt["artifact"]["sha256"],
                "simulator_diagnostic_only": True,
                "voting_result": False,
                "deployment_allowed": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


__all__: Sequence[str] = (
    "DEFAULT_OUTPUT",
    "EXPECTED_CAT2_ROLLOUT_COUNT",
    "EXPECTED_PROXY_EVENT_COUNT",
    "MECHANISM_CLASSIFICATION",
    "NOMINAL_RETRY_WAIT_MS",
    "NoopAdjudicationError",
    "NoopEvidenceAccumulator",
    "OLD_REASON",
    "TIMING_PROXY_REASON",
    "adjudicate_step",
    "build_adjudication_artifact",
    "capture_sidecar_sources",
    "expected_full_proxy_count",
    "main",
    "replay_cat2_for_adjudication",
    "write_adjudication_with_receipt",
)


if __name__ == "__main__":
    raise SystemExit(main())
