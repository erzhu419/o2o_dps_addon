"""Compare the eight reviewed Phase-12 Fury facts with the local simulator.

This is an independent, read-only comparison stage.  It consumes the existing
evidence review and inspects or executes the local wowsims-turtle tree, but it
does not rewrite the review, change a mechanics registry, patch the simulator,
or turn an unmeasured value into a simulator claim.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REVIEW = (
    PROJECT_ROOT
    / "offline_data"
    / "sim_validation"
    / "fury_current_build_phase12_review.json"
)
DEFAULT_WOWSIMS_ROOT = PROJECT_ROOT.parent / "wowsims-turtle"
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "offline_data"
    / "sim_validation"
    / "fury_current_build_phase12_comparison.json"
)

SCHEMA_VERSION = 1
COMPARATOR = "fury_current_build_phase12_simulator_comparison_v1"
EXPECTED_REVIEW_KIND = "fury_current_build_phase12_evidence_review"
SLAM_RUNTIME_TEST = "TestTurtleSlamCanCastWhileMoving"
COMPARISON_RESULTS = ("MATCH", "MISMATCH", "NOT_COMPARABLE")
EVIDENCE_CHANNELS = ("runtime_test", "source_model", "interface_gap")
REVIEW_KEYS = (
    "movement_slam",
    "whirlwind_no_current_target_hit",
    "stance_rage_retention",
    "death_wish_recklessness_duration",
    "queue_cross_stance",
    "bloodrage_event_contract",
    "flurry_death_wish_qualitative",
    "dual_wield_qualitative",
)


class FuryPhase12ComparisonError(RuntimeError):
    """The review or simulator tree cannot support the bounded comparison."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FuryPhase12ComparisonError(
            f"cannot read {label} {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise FuryPhase12ComparisonError(f"{label} is not a JSON object: {path}")
    return value


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise FuryPhase12ComparisonError(f"{field} must be a string array")
    return list(value)


def _validate_review(review: Mapping[str, Any]) -> Mapping[str, Any]:
    if review.get("kind") != EXPECTED_REVIEW_KIND:
        raise FuryPhase12ComparisonError(
            "input is not a Phase-12 Fury evidence review"
        )
    reviews = review.get("evidence_reviews")
    if not isinstance(reviews, Mapping) or set(reviews) != set(REVIEW_KEYS):
        raise FuryPhase12ComparisonError(
            "review must contain exactly the eight Phase-12 evidence keys"
        )

    publication = _mapping(review.get("publication_gate"))
    if (
        publication.get("simulator_patch_allowed") is not False
        or publication.get("simulator_patch") is not None
    ):
        raise FuryPhase12ComparisonError(
            "source review does not preserve the no-patch boundary"
        )
    if review.get("mechanics_registry_modified") is not False:
        raise FuryPhase12ComparisonError("source review modifies the mechanics registry")
    if review.get("simulator_modified") is not False:
        raise FuryPhase12ComparisonError("source review modifies the simulator")
    if review.get("mechanics_registry_mutations") != []:
        raise FuryPhase12ComparisonError(
            "source review contains mechanics-registry mutations"
        )
    if review.get("simulator_overrides") != []:
        raise FuryPhase12ComparisonError("source review contains simulator overrides")

    for key in REVIEW_KEYS:
        item = _mapping(reviews.get(key))
        observed = _mapping(item.get("observed_fact"))
        _string_list(observed.get("claims"), f"evidence_reviews.{key}.claims")
        promotion = _mapping(item.get("promotion_decision"))
        if promotion.get("decision") != "PROMOTE_OBSERVED_FACT_ONLY":
            raise FuryPhase12ComparisonError(
                f"evidence review {key!r} is not a promoted observed fact"
            )
        if (
            promotion.get("mechanics_registry_modified") is not False
            or promotion.get("simulator_modified") is not False
            or promotion.get("simulator_override") is not None
        ):
            raise FuryPhase12ComparisonError(
                f"evidence review {key!r} crosses the no-mutation boundary"
            )
        simulator = _mapping(item.get("simulator_comparison"))
        if (
            simulator.get("status") != "NOT_EVALUATED"
            or simulator.get("simulator_value") is not None
            or simulator.get("comparison_result") is not None
        ):
            raise FuryPhase12ComparisonError(
                f"evidence review {key!r} already contains a simulator conclusion"
            )
    return reviews


def _source_file(root: Path, relative: str) -> tuple[Path, str]:
    path = (root / Path(relative)).resolve()
    if not path.is_file():
        raise FuryPhase12ComparisonError(f"wowsims source file is missing: {path}")
    try:
        return path, path.read_text(encoding="utf-8")
    except OSError as error:
        raise FuryPhase12ComparisonError(
            f"cannot read wowsims source file {path}: {error}"
        ) from error


def _line_observation(path: Path, text: str, needle: str) -> dict[str, Any] | None:
    for line_number, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return {
                "path": str(path),
                "line": line_number,
                "text": line.strip(),
            }
    return None


def _source_channel(
    *,
    status: str,
    observations: Sequence[dict[str, Any]],
    reason: str,
) -> dict[str, Any]:
    return {
        "channel": "source_model",
        "status": status,
        "observations": list(observations),
        "reason": reason,
    }


def _interface_gap(*reasons: str) -> dict[str, Any]:
    return {
        "channel": "interface_gap",
        "status": "BLOCKING",
        "reasons": list(reasons),
    }


def _run_slam_runtime_test(
    wowsims_root: Path, go_executable: Path | None
) -> dict[str, Any]:
    command_tail = [
        "test",
        "--tags=with_db",
        "-count=1",
        "-v",
        "-run",
        f"^{SLAM_RUNTIME_TEST}$",
        "./sim/o2o",
    ]
    if go_executable is None:
        return {
            "channel": "runtime_test",
            "status": "NOT_RUN",
            "test": SLAM_RUNTIME_TEST,
            "command": None,
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "reason": "no Go executable was supplied",
        }

    executable = go_executable.expanduser().resolve()
    command = [str(executable), *command_tail]
    if not executable.is_file():
        return {
            "channel": "runtime_test",
            "status": "NOT_RUN",
            "test": SLAM_RUNTIME_TEST,
            "command": command,
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "reason": f"Go executable does not exist: {executable}",
        }

    environment = os.environ.copy()
    environment["GOTOOLCHAIN"] = "local"
    environment["CGO_ENABLED"] = "0"
    possible_go_root = executable.parent.parent
    if (possible_go_root / "src").is_dir():
        environment["GOROOT"] = str(possible_go_root)
        environment["PATH"] = str(executable.parent) + os.pathsep + environment.get(
            "PATH", ""
        )

    try:
        with tempfile.TemporaryDirectory(prefix="o2o-phase12-go-build-") as cache:
            environment["GOCACHE"] = cache
            completed = subprocess.run(
                command,
                cwd=wowsims_root,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
    except OSError as error:
        return {
            "channel": "runtime_test",
            "status": "NOT_RUN",
            "test": SLAM_RUNTIME_TEST,
            "command": command,
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "reason": f"could not start Go test: {error}",
        }

    pass_marker = re.search(
        rf"^--- PASS: {re.escape(SLAM_RUNTIME_TEST)}(?:\s|$)",
        completed.stdout,
        flags=re.MULTILINE,
    )
    passed = completed.returncode == 0 and pass_marker is not None
    return {
        "channel": "runtime_test",
        "status": "PASS" if passed else "FAIL",
        "test": SLAM_RUNTIME_TEST,
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "reason": (
            "the exact named Windows Go runtime test passed"
            if passed
            else "the exact named Windows Go runtime test did not produce a passing result"
        ),
    }


def _source_models(wowsims_root: Path) -> dict[str, Any]:
    slam_path, slam_text = _source_file(wowsims_root, "sim/warrior/slam.go")
    slam_test_path, slam_test_text = _source_file(
        wowsims_root, "sim/o2o/slam_test.go"
    )
    whirlwind_path, whirlwind_text = _source_file(
        wowsims_root, "sim/warrior/whirlwind.go"
    )
    stances_path, stances_text = _source_file(
        wowsims_root, "sim/warrior/stances.go"
    )
    recklessness_path, recklessness_text = _source_file(
        wowsims_root, "sim/warrior/recklessness.go"
    )
    talents_path, talents_text = _source_file(
        wowsims_root, "sim/warrior/talents.go"
    )
    queue_path, queue_text = _source_file(
        wowsims_root, "sim/warrior/heroic_strike_cleave.go"
    )
    bloodrage_path, bloodrage_text = _source_file(
        wowsims_root, "sim/warrior/bloodrage.go"
    )

    slam_needles = (
        "SpellFlagCastWhileMoving",
        f"func {SLAM_RUNTIME_TEST}",
    )
    slam_observations = [
        observation
        for observation in (
            _line_observation(slam_path, slam_text, slam_needles[0]),
            _line_observation(slam_test_path, slam_test_text, slam_needles[1]),
        )
        if observation is not None
    ]

    whirlwind_needles = (
        "results := make([]*core.SpellResult, min(4, warrior.Env.GetNumTargets()))",
        "for idx := range results",
        "for _, result := range results",
    )
    whirlwind_observations = [
        observation
        for needle in whirlwind_needles
        if (
            observation := _line_observation(
                whirlwind_path, whirlwind_text, needle
            )
        )
        is not None
    ]
    whirlwind_distance_tokens = (
        "DistanceFromTarget",
        "MaxMeleeAttackDistance",
        "SpellRange",
    )
    whirlwind_recognized = (
        len(whirlwind_observations) == len(whirlwind_needles)
        and not any(token in whirlwind_text for token in whirlwind_distance_tokens)
    )

    stance_observation = _line_observation(
        stances_path,
        stances_text,
        "maxRetainedRage := 5 * float64(warrior.Talents.TacticalMastery)",
    )

    recklessness_comment = _line_observation(
        recklessness_path,
        recklessness_text,
        "increases critical strike chance by 50%",
    )
    recklessness_duration = _line_observation(
        recklessness_path, recklessness_text, "Duration: time.Second * 15"
    )
    recklessness_crit = _line_observation(
        recklessness_path,
        recklessness_text,
        "100*core.CritRatingPerCritChance",
    )
    recklessness_cooldown = _line_observation(
        recklessness_path, recklessness_text, "Duration: time.Minute * 30"
    )
    death_wish_duration = _line_observation(
        talents_path, talents_text, "Duration: time.Second * 30"
    )
    death_wish_multiplier = _line_observation(
        talents_path,
        talents_text,
        "SchoolDamageDealtMultiplier[stats.SchoolIndexPhysical] *= 1.2",
    )
    recklessness_conflict = all(
        observation is not None
        for observation in (
            recklessness_comment,
            recklessness_duration,
            recklessness_crit,
            recklessness_cooldown,
        )
    )

    queue_any_stance = _line_observation(
        queue_path,
        queue_text,
        "queueSpell := warrior.RegisterSpell(AnyStance",
    )
    queue_miss_rule = _line_observation(
        queue_path, queue_text, "DisableDWMissPenalty = true"
    )
    queue_miss_restore = _line_observation(
        queue_path, queue_text, "DisableDWMissPenalty = false"
    )

    bloodrage_instant = _line_observation(
        bloodrage_path,
        bloodrage_text,
        "instantRage := 10.0 + []float64{0, 2, 5}",
    )
    bloodrage_ticks = _line_observation(
        bloodrage_path, bloodrage_text, "NumTicks: 10"
    )
    bloodrage_period = _line_observation(
        bloodrage_path, bloodrage_text, "Period:   time.Second * 1"
    )
    bloodrage_action = _line_observation(
        bloodrage_path, bloodrage_text, "actionID := core.ActionID{SpellID: 2687}"
    )
    bloodrage_periodic_spell_present = "29131" in bloodrage_text

    flurry_speed = _line_observation(
        talents_path,
        talents_text,
        "attackSpeed := []float64{1.1, 1.15, 1.2, 1.25, 1.3}",
    )
    flurry_apply = _line_observation(
        talents_path, talents_text, "warrior.MultiplyMeleeSpeed(sim, attackSpeed)"
    )

    return {
        "movement_slam": {
            "recognized": len(slam_observations) == len(slam_needles),
            "channel": _source_channel(
                status=(
                    "INSPECTED"
                    if len(slam_observations) == len(slam_needles)
                    else "UNRECOGNIZED"
                ),
                observations=slam_observations,
                reason=(
                    "Slam declares cast-while-moving support and the exact runtime test exists."
                ),
            ),
            "simulator_value": {
                "cast_while_moving_flag_present": (
                    len(slam_observations) == len(slam_needles)
                )
            },
        },
        "whirlwind_no_current_target_hit": {
            "recognized": whirlwind_recognized,
            "channel": _source_channel(
                status="INSPECTED" if whirlwind_recognized else "UNRECOGNIZED",
                observations=whirlwind_observations,
                reason=(
                    "Whirlwind allocates one result per encounter target up to four and "
                    "contains no distance or eligibility filter in this spell model."
                ),
            ),
            "simulator_value": {
                "result_cardinality": "min(4, encounter_target_count)",
                "distance_filter_present": False,
                "target_eligibility_filter_present": False,
                "all_allocated_results_processed": (
                    len(whirlwind_observations) == len(whirlwind_needles)
                ),
            },
        },
        "stance_rage_retention": {
            "recognized": stance_observation is not None,
            "channel": _source_channel(
                status="INSPECTED" if stance_observation else "UNRECOGNIZED",
                observations=[stance_observation] if stance_observation else [],
                reason="The simulator retains at most five rage per Tactical Mastery rank.",
            ),
            "simulator_value": {
                "retained_rage_cap_formula": "5 * TacticalMastery"
                if stance_observation
                else None
            },
        },
        "death_wish_recklessness_duration": {
            "recognized": all(
                observation is not None
                for observation in (death_wish_duration, death_wish_multiplier)
            ),
            "channel": _source_channel(
                status="INTERNAL_CONFLICT" if recklessness_conflict else "INSPECTED",
                observations=[
                    observation
                    for observation in (
                        death_wish_duration,
                        death_wish_multiplier,
                        recklessness_comment,
                        recklessness_duration,
                        recklessness_crit,
                        recklessness_cooldown,
                    )
                    if observation is not None
                ],
                reason=(
                    "Recklessness source commentary says 50% crit, 12 seconds, and "
                    "5 minutes, while executable fields use 100%, 15 seconds, and "
                    "30 minutes. The live review did not measure either duration."
                ),
            ),
            "simulator_value": {
                "death_wish": {
                    "duration_seconds": 30 if death_wish_duration else None,
                    "physical_damage_multiplier": 1.2
                    if death_wish_multiplier
                    else None,
                },
                "recklessness_comment": {
                    "crit_bonus_percent": 50,
                    "duration_seconds": 12,
                    "cooldown_minutes": 5,
                }
                if recklessness_comment
                else None,
                "recklessness_implementation": {
                    "crit_bonus_percent": 100,
                    "duration_seconds": 15,
                    "cooldown_minutes": 30,
                }
                if all(
                    observation is not None
                    for observation in (
                        recklessness_duration,
                        recklessness_crit,
                        recklessness_cooldown,
                    )
                )
                else None,
                "source_internal_conflict": recklessness_conflict,
            },
        },
        "queue_cross_stance": {
            "recognized": queue_any_stance is not None,
            "channel": _source_channel(
                status="INSPECTED" if queue_any_stance else "UNRECOGNIZED",
                observations=[queue_any_stance] if queue_any_stance else [],
                reason=(
                    "The queue action is registered for AnyStance, but source inspection "
                    "is not a cross-stance runtime replay."
                ),
            ),
            "simulator_value": {
                "queue_action_stance_mask": "AnyStance"
                if queue_any_stance
                else None
            },
        },
        "bloodrage_event_contract": {
            "recognized": all(
                observation is not None
                for observation in (
                    bloodrage_instant,
                    bloodrage_ticks,
                    bloodrage_period,
                    bloodrage_action,
                )
            ),
            "channel": _source_channel(
                status="INSPECTED",
                observations=[
                    observation
                    for observation in (
                        bloodrage_action,
                        bloodrage_instant,
                        bloodrage_ticks,
                        bloodrage_period,
                    )
                    if observation is not None
                ],
                reason=(
                    "The simulator models an immediate gain and ten one-second ticks, "
                    "but this source uses one 2687 rage-metrics identity and contains no "
                    "29131 event identity."
                ),
            ),
            "simulator_value": {
                "immediate_rage": "10 + ImprovedBloodrage bonus",
                "periodic_ticks": 10 if bloodrage_ticks else None,
                "period_seconds": 1 if bloodrage_period else None,
                "rage_metrics_spell_id": 2687 if bloodrage_action else None,
                "periodic_event_spell_id_29131_present": bloodrage_periodic_spell_present,
            },
        },
        "flurry_death_wish_qualitative": {
            "recognized": all(
                observation is not None
                for observation in (flurry_speed, flurry_apply, death_wish_multiplier)
            ),
            "channel": _source_channel(
                status="INSPECTED",
                observations=[
                    observation
                    for observation in (
                        flurry_speed,
                        flurry_apply,
                        death_wish_multiplier,
                    )
                    if observation is not None
                ],
                reason=(
                    "The source contains numeric Flurry and Death Wish effects, while "
                    "the live review retained event ordering only."
                ),
            ),
            "simulator_value": {
                "flurry_rank_multipliers": [1.1, 1.15, 1.2, 1.25, 1.3]
                if flurry_speed
                else None,
                "death_wish_physical_damage_multiplier": 1.2
                if death_wish_multiplier
                else None,
            },
        },
        "dual_wield_qualitative": {
            "recognized": all(
                observation is not None
                for observation in (queue_miss_rule, queue_miss_restore)
            ),
            "channel": _source_channel(
                status="INSPECTED",
                observations=[
                    observation
                    for observation in (queue_miss_rule, queue_miss_restore)
                    if observation is not None
                ],
                reason=(
                    "The simulator disables the dual-wield miss penalty while a queued "
                    "next-swing aura is active, but the live review contains no hit-rate fit."
                ),
            ),
            "simulator_value": {
                "queued_next_swing_disables_dw_miss_penalty": True
                if queue_miss_rule
                else None,
                "queue_expiry_restores_dw_miss_penalty": True
                if queue_miss_restore
                else None,
            },
        },
    }


def _review_projection(item: Mapping[str, Any]) -> dict[str, Any]:
    observed = _mapping(item.get("observed_fact"))
    promotion = _mapping(item.get("promotion_decision"))
    return {
        "question": item.get("question"),
        "observed_status": observed.get("status"),
        "observed_claims": _string_list(observed.get("claims"), "observed claims"),
        "gate": observed.get("gate"),
        "gate_status": observed.get("gate_status"),
        "completed_support_stage_ids": list(
            observed.get("completed_support_stage_ids", [])
        ),
        "historically_adjudicated_support_stage_ids": list(
            observed.get("historically_adjudicated_support_stage_ids", [])
        ),
        "incomplete_or_missing_stage_ids": list(
            observed.get("incomplete_or_missing_stage_ids", [])
        ),
        "scope_limit": observed.get("scope_limit"),
        "unresolved": list(promotion.get("unresolved", [])),
    }


def _entry(
    item: Mapping[str, Any],
    *,
    result: str,
    deciding_channel: str,
    reason: str,
    simulator_value: Mapping[str, Any],
    evidence_channels: Sequence[Mapping[str, Any]],
    comparison_scope: str,
) -> dict[str, Any]:
    if result not in COMPARISON_RESULTS:
        raise FuryPhase12ComparisonError(f"unsupported comparison result: {result}")
    if deciding_channel not in EVIDENCE_CHANNELS:
        raise FuryPhase12ComparisonError(
            f"unsupported deciding evidence channel: {deciding_channel}"
        )
    projection = _review_projection(item)
    return {
        **projection,
        "comparison_result": result,
        "deciding_channel": deciding_channel,
        "comparison_scope": comparison_scope,
        "simulator_value": dict(simulator_value),
        "evidence_channels": [dict(channel) for channel in evidence_channels],
        "reason": reason,
        "replacement_formula_identified": False,
        "simulator_patch_allowed": False,
        "simulator_patch": None,
    }


def build_phase12_comparison(
    review: Mapping[str, Any],
    *,
    review_path: Path,
    wowsims_root: Path,
    go_executable: Path | None = None,
) -> dict[str, Any]:
    reviews = _validate_review(review)
    root = wowsims_root.expanduser().resolve()
    if not root.is_dir():
        raise FuryPhase12ComparisonError(f"wowsims root is not a directory: {root}")

    models = _source_models(root)
    slam_runtime = _run_slam_runtime_test(root, go_executable)
    slam_model = models["movement_slam"]
    slam_passed = (
        slam_runtime.get("status") == "PASS" and slam_model.get("recognized") is True
    )

    comparisons: dict[str, dict[str, Any]] = {}
    comparisons["movement_slam"] = _entry(
        _mapping(reviews["movement_slam"]),
        result="MATCH" if slam_passed else "NOT_COMPARABLE",
        deciding_channel="runtime_test",
        reason=(
            "The exact Windows Go runtime test passed and confirms the promoted "
            "moving-Slam castability claim."
            if slam_passed
            else "A source declaration alone is insufficient: the exact Windows Go "
            "runtime test was not run successfully, so movement Slam cannot be MATCH."
        ),
        simulator_value={
            **slam_model["simulator_value"],
            "runtime_test_passed": slam_passed,
        },
        evidence_channels=(slam_model["channel"], slam_runtime),
        comparison_scope=(
            "castability while moving only; no numeric cast-time or interruption rule"
        ),
    )

    whirlwind_model = models["whirlwind_no_current_target_hit"]
    whirlwind_recognized = whirlwind_model.get("recognized") is True
    comparisons["whirlwind_no_current_target_hit"] = _entry(
        _mapping(reviews["whirlwind_no_current_target_hit"]),
        result="MISMATCH" if whirlwind_recognized else "NOT_COMPARABLE",
        deciding_channel=("source_model" if whirlwind_recognized else "interface_gap"),
        reason=(
            "The promoted live fact separates successful cast from zero eligible targets, "
            "while the current Whirlwind source creates and processes results from the "
            "encounter target list without a distance/eligibility filter. This identifies "
            "a scoped model mismatch, not an exact radius or replacement implementation."
            if whirlwind_recognized
            else "The expected Whirlwind source model was not recognized, so no mismatch "
            "can be asserted."
        ),
        simulator_value=whirlwind_model["simulator_value"],
        evidence_channels=(
            whirlwind_model["channel"],
            _interface_gap(
                "the live evidence does not identify the exact Whirlwind radius",
                "the live evidence does not identify target ordering or eligibility",
            ),
        ),
        comparison_scope="cast-versus-target-inclusion semantics only",
    )

    interface_specs = {
        "stance_rage_retention": (
            "The live exact-action chain observes stance transitions but no rage delta, "
            "so the simulator retained-rage formula cannot be compared.",
            (
                "live rage before and after each stance transition is absent",
                "the retained-rage cap is not measured for this build",
            ),
            "source formula inventory only",
        ),
        "death_wish_recklessness_duration": (
            "The live review observes activation auras but no removal timestamp or duration. "
            "The Recklessness comment/implementation conflict is recorded as a source issue, "
            "not resolved in favor of either value.",
            (
                "live Death Wish and Recklessness durations are absent",
                "no live multiplier or cooldown measurement selects either Recklessness value set",
            ),
            "source values and source-internal conflict inventory only",
        ),
        "queue_cross_stance": (
            "The source registers the queue action for AnyStance, but no exact simulator "
            "cross-stance runtime replay was executed by this first comparison stage.",
            (
                "source inspection is not a runtime replay of queue then stance then swing",
                "queue cancellation and target-switch siblings remain unresolved",
            ),
            "source queue stance-mask inventory only",
        ),
        "bloodrage_event_contract": (
            "The live review distinguishes event families 2687 and 29131, while the current "
            "comparison interface exports state rather than per-event rage identities.",
            (
                "simulator event-family trace is not exposed to this comparison",
                "live unique tick count and applied rage per event are not measured",
            ),
            "source periodic-action inventory only",
        ),
        "flurry_death_wish_qualitative": (
            "The live evidence establishes ordering only; it does not measure the numeric "
            "Flurry haste or Death Wish damage values present in simulator source.",
            (
                "live Flurry haste multiplier is absent",
                "live numeric swing-timer rescaling and Death Wish multiplier are absent",
            ),
            "source multiplier inventory only",
        ),
        "dual_wield_qualitative": (
            "The live evidence proves swing presence but contains no per-hand hit-rate fit, "
            "so the queued dual-wield miss rule cannot be compared.",
            (
                "live unqueued and queued off-hand miss-rate samples are absent",
                "the current comparison has no matched dual-wield runtime request",
            ),
            "source queued-miss-rule inventory only",
        ),
    }
    for key, (reason, gaps, scope) in interface_specs.items():
        model = models[key]
        comparisons[key] = _entry(
            _mapping(reviews[key]),
            result="NOT_COMPARABLE",
            deciding_channel="interface_gap",
            reason=reason,
            simulator_value=model["simulator_value"],
            evidence_channels=(model["channel"], _interface_gap(*gaps)),
            comparison_scope=scope,
        )

    if tuple(comparisons) != REVIEW_KEYS:
        raise FuryPhase12ComparisonError(
            "comparison output drifted from the exact eight-key contract"
        )
    counts = Counter(
        comparison["comparison_result"] for comparison in comparisons.values()
    )
    for result in COMPARISON_RESULTS:
        counts.setdefault(result, 0)

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "fury_current_build_phase12_simulator_comparison",
        "comparator": COMPARATOR,
        "created_at": _utc_now(),
        "status": "PARTIAL_COMPARISON",
        "sources": {
            "review": str(review_path.expanduser().resolve()),
            "wowsims_root": str(root),
            "go_executable": (
                str(go_executable.expanduser().resolve())
                if go_executable is not None
                else None
            ),
        },
        "comparison_contract": {
            "review_key_order": list(REVIEW_KEYS),
            "allowed_results": list(COMPARISON_RESULTS),
            "evidence_channels": list(EVIDENCE_CHANNELS),
            "movement_slam_match_requires_exact_runtime_test_pass": True,
            "source_model_mismatch_does_not_identify_replacement": True,
            "not_comparable_never_authorizes_patch": True,
        },
        "comparisons": comparisons,
        "summary": {
            "comparison_count": len(comparisons),
            "result_counts": {
                result: counts[result] for result in COMPARISON_RESULTS
            },
            "slam_runtime_test_status": slam_runtime.get("status"),
            "whirlwind_source_model_mismatch": (
                comparisons["whirlwind_no_current_target_hit"][
                    "comparison_result"
                ]
                == "MISMATCH"
            ),
            "recklessness_source_internal_conflict": models[
                "death_wish_recklessness_duration"
            ]["simulator_value"]["source_internal_conflict"],
        },
        "publication_gate": {
            "simulator_comparison_complete": False,
            "replacement_formula_identified": False,
            "simulator_patch_allowed": False,
            "simulator_patch": None,
            "reason": (
                "The comparison leaves "
                f"{counts['NOT_COMPARABLE']} fact(s) not comparable; the scoped "
                "Whirlwind source-model mismatch does not identify a replacement, and "
                "no replacement formula or held-out validation exists."
            ),
        },
        "mechanics_registry_mutations": [],
        "simulator_overrides": [],
        "mechanics_registry_modified": False,
        "simulator_modified": False,
    }


def run_from_paths(
    review_path: str | Path = DEFAULT_REVIEW,
    *,
    wowsims_root: str | Path = DEFAULT_WOWSIMS_ROOT,
    go_executable: str | Path | None = None,
    output: str | Path | None = None,
) -> dict[str, Any]:
    review_file = Path(review_path).expanduser().resolve()
    wowsims_directory = Path(wowsims_root).expanduser().resolve()
    go_file = (
        Path(go_executable).expanduser().resolve()
        if go_executable is not None
        else None
    )
    document = build_phase12_comparison(
        _load_object(review_file, "Phase-12 evidence review"),
        review_path=review_file,
        wowsims_root=wowsims_directory,
        go_executable=go_file,
    )
    if output is not None:
        destination = Path(output).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".tmp")
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False)
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(destination)
    return document


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review", type=Path, default=DEFAULT_REVIEW)
    parser.add_argument("--wowsims-root", type=Path, default=DEFAULT_WOWSIMS_ROOT)
    parser.add_argument(
        "--go-executable",
        type=Path,
        help=(
            "Windows go.exe used to run the exact moving-Slam test; when omitted, "
            "movement_slam remains NOT_COMPARABLE"
        ),
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        document = run_from_paths(
            args.review,
            wowsims_root=args.wowsims_root,
            go_executable=args.go_executable,
            output=args.output,
        )
    except (FuryPhase12ComparisonError, OSError) as error:
        print(f"Phase-12 simulator comparison failed: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": document["status"],
                "output": str(args.output.resolve()),
                "result_counts": document["summary"]["result_counts"],
                "slam_runtime_test_status": document["summary"][
                    "slam_runtime_test_status"
                ],
                "simulator_comparison_complete": False,
                "replacement_formula_identified": False,
                "simulator_patch_allowed": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


__all__ = [
    "COMPARATOR",
    "COMPARISON_RESULTS",
    "DEFAULT_OUTPUT",
    "DEFAULT_REVIEW",
    "DEFAULT_WOWSIMS_ROOT",
    "EVIDENCE_CHANNELS",
    "FuryPhase12ComparisonError",
    "REVIEW_KEYS",
    "SLAM_RUNTIME_TEST",
    "build_phase12_comparison",
    "run_from_paths",
]


if __name__ == "__main__":
    raise SystemExit(main())
