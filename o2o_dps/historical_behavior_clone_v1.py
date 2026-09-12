"""Executable pooled-clean-Fury behavior clone for simulator diagnostics.

The Chronicle policy diagnostics predict an action only at rows where an
action was observed.  Calling such a classifier on every simulator tick turns
the log into an unrealistic spam policy.  This module closes that specific
gap with a small marked semi-Markov model:

* the delay head schedules the next decision from observed inter-action gaps;
* the mark head predicts exactly the 15 controllable Turtle Warrior actions;
* the target head predicts the relative target role; and
* runtime action probabilities are conditioned on the simulator's legal mask.

The training cohort is deliberately named ``POOLED_CLEAN_FURY``.  It is not a
top-player policy and carries no comparison, voting, or deployment claim.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass, field
import json
import math
import random
from typing import Any, Iterable, Mapping, Sequence

from . import chronicle_external_historical_fury_policy_v2 as policy_v2
from . import chronicle_external_historical_fury_policy_v3 as policy_v3
from . import chronicle_external_historical_fury_policy_v4 as policy_v4
from . import chronicle_external_teammate_response_model_v1 as teammate_v1


MODEL_SCHEMA = "historical_behavior_clone/v1"
OBSERVATION_SCHEMA = "historical_behavior_clone_runtime_observation/v1"
STATUS = "DEVELOPMENT_ONLY_POOLED_CLEAN_FURY_NONVOTING"
COHORT_ID = "POOLED_CLEAN_FURY"
START_ACTION = "__WAVE_START__"

ACTION_KEYS = tuple(spec.action_key for spec in policy_v3.ACTION_ONTOLOGY)
ACTION_SPEC_BY_KEY = {spec.action_key: spec for spec in policy_v3.ACTION_ONTOLOGY}
STANCE_ACTION_KEYS = frozenset(
    {
        "warrior.battle_stance",
        "warrior.defensive_stance",
        "warrior.berserker_stance",
    }
)
TARGET_ROLES = (
    "NO_EXPLICIT_TARGET",
    "SELF",
    "CURRENT_ENEMY",
    "OTHER_OR_NEW_ENEMY",
    "OTHER_FRIENDLY",
)
DELAY_UPPER_BOUNDS_MS = teammate_v1.DELAY_UPPER_BOUNDS_MS
DEFAULT_ALPHA = 1.0
DEFAULT_BACKOFF_STRENGTH = 20.0


class HistoricalBehaviorCloneV1Error(ValueError):
    """A training row, model, observation, or runtime transition is invalid."""


@dataclass(frozen=True)
class TrainingDecisionV1:
    """One strict-prefix action label in a single player-wave episode."""

    elapsed_ms: int
    action_key: str
    target_role: str
    feature_atoms: tuple[policy_v4.FeatureAtom, ...] = ()


@dataclass
class _DelayCell:
    counts: Counter[str] = field(default_factory=Counter)
    delay_sum_ms: Counter[str] = field(default_factory=Counter)

    def add(self, delay_ms: int) -> None:
        bucket = _delay_bucket(delay_ms)
        self.counts[bucket] += 1
        self.delay_sum_ms[bucket] += delay_ms


def _canonical_text(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise HistoricalBehaviorCloneV1Error(f"{label} must be a nonnegative integer")
    return value


def _finite_nonnegative(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HistoricalBehaviorCloneV1Error(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise HistoricalBehaviorCloneV1Error(f"{label} must be finite and nonnegative")
    return result


def _delay_bucket(delay_ms: int) -> str:
    value = _nonnegative_int(delay_ms, "delay_ms")
    for upper in DELAY_UPPER_BOUNDS_MS:
        if value <= upper:
            return f"LE_{upper}"
    return f"GT_{DELAY_UPPER_BOUNDS_MS[-1]}"


def _delay_cell_wire(cell: _DelayCell) -> dict[str, Any]:
    rows = []
    for bucket in _delay_bucket_order():
        count = cell.counts.get(bucket, 0)
        if not count:
            continue
        total = cell.delay_sum_ms[bucket]
        rows.append(
            {
                "bucket": bucket,
                "count": count,
                "delay_sum_ms": total,
                "representative_ms": int(round(total / count)),
            }
        )
    return {"sample_count": sum(cell.counts.values()), "buckets": rows}


def _delay_bucket_order() -> tuple[str, ...]:
    return tuple(f"LE_{value}" for value in DELAY_UPPER_BOUNDS_MS) + (
        f"GT_{DELAY_UPPER_BOUNDS_MS[-1]}",
    )


def _count_wire(counter: Mapping[str, int], keys: Sequence[str]) -> dict[str, int]:
    return {key: int(counter.get(key, 0)) for key in keys}


def _validate_training_decision(
    decision: TrainingDecisionV1, *, previous_elapsed_ms: int | None
) -> None:
    _nonnegative_int(decision.elapsed_ms, "training elapsed_ms")
    if previous_elapsed_ms is not None and decision.elapsed_ms < previous_elapsed_ms:
        raise HistoricalBehaviorCloneV1Error(
            "training decisions must be monotone within an episode"
        )
    if decision.action_key not in ACTION_SPEC_BY_KEY:
        raise HistoricalBehaviorCloneV1Error(
            f"training action is outside the 15-action ontology: {decision.action_key}"
        )
    if decision.target_role not in TARGET_ROLES:
        raise HistoricalBehaviorCloneV1Error(
            f"unsupported training target role: {decision.target_role}"
        )
    families: set[str] = set()
    for atom in decision.feature_atoms:
        if not isinstance(atom, policy_v4.FeatureAtom):
            raise HistoricalBehaviorCloneV1Error(
                "training feature atoms must be V4 FeatureAtom values"
            )
        if atom.family in families:
            raise HistoricalBehaviorCloneV1Error(
                f"duplicate training feature family: {atom.family}"
            )
        families.add(atom.family)


def compile_training_episodes_v1(
    episodes: Iterable[Sequence[TrainingDecisionV1]],
    *,
    source_audit: Mapping[str, Any] | None = None,
    alpha: float = DEFAULT_ALPHA,
    backoff_strength: float = DEFAULT_BACKOFF_STRENGTH,
) -> dict[str, Any]:
    """Compile normalized clean-Fury episodes into a JSON-serializable model."""

    alpha = _finite_nonnegative(alpha, "alpha")
    strength = _finite_nonnegative(backoff_strength, "backoff_strength")
    if alpha == 0 or strength == 0:
        raise HistoricalBehaviorCloneV1Error(
            "alpha and backoff_strength must be strictly positive"
        )

    action_counts: Counter[str] = Counter()
    context_counts: dict[str, dict[str, Counter[str]]] = defaultdict(
        lambda: defaultdict(Counter)
    )
    target_counts: dict[str, Counter[str]] = defaultdict(Counter)
    delay_global = _DelayCell()
    delay_by_previous: dict[str, _DelayCell] = defaultdict(_DelayCell)
    episode_count = 0
    decision_count = 0

    for raw_episode in episodes:
        episode = list(raw_episode)
        if not episode:
            continue
        episode_count += 1
        previous_elapsed = 0
        previous_action = START_ACTION
        for decision in episode:
            _validate_training_decision(
                decision, previous_elapsed_ms=previous_elapsed
            )
            delay_ms = decision.elapsed_ms - previous_elapsed
            delay_global.add(delay_ms)
            delay_by_previous[previous_action].add(delay_ms)
            action_counts[decision.action_key] += 1
            target_counts[decision.action_key][decision.target_role] += 1
            for atom in decision.feature_atoms:
                context_counts[atom.family][atom.value][decision.action_key] += 1
            decision_count += 1
            previous_elapsed = decision.elapsed_ms
            previous_action = decision.action_key

    if not decision_count:
        raise HistoricalBehaviorCloneV1Error(
            "at least one clean controllable training decision is required"
        )

    model = {
        "schema": MODEL_SCHEMA,
        "status": STATUS,
        "cohort_contract": {
            "cohort_id": COHORT_ID,
            "population": (
                "all supplied Stage5 Warrior/Fury player-wave episodes passing "
                "historical_fury_candidate_filter_passed"
            ),
            "top_player_selection_used": False,
            "historical_dps_ranking_used": False,
            "same_loadout_claim": (
                "runtime mechanics may use a shared simulator loadout; learned "
                "behavior remains pooled across historical loadouts"
            ),
        },
        "action_ontology": [
            {
                "action_key": spec.action_key,
                "lane": spec.lane,
                "unpaired_go_is_decision": spec.unpaired_go_is_decision,
            }
            for spec in policy_v3.ACTION_ONTOLOGY
        ],
        "delay_head": {
            "kind": "PREVIOUS_MARK_CONDITIONAL_EMPIRICAL_SEMI_MARKOV",
            "upper_bounds_ms": list(DELAY_UPPER_BOUNDS_MS),
            "backoff_strength": strength,
            "global": _delay_cell_wire(delay_global),
            "by_previous_action": {
                key: _delay_cell_wire(value)
                for key, value in sorted(delay_by_previous.items())
            },
            "wave_start_key": START_ACTION,
        },
        "mark_head": {
            "kind": "INDEPENDENT_FAMILY_BACKOFF",
            "alpha": alpha,
            "backoff_strength": strength,
            "global_counts": _count_wire(action_counts, ACTION_KEYS),
            "context_counts": {
                family: {
                    value: _count_wire(counts, ACTION_KEYS)
                    for value, counts in sorted(values.items())
                }
                for family, values in sorted(context_counts.items())
            },
        },
        "target_head": {
            "kind": "ACTION_CONDITIONAL_CATEGORICAL",
            "alpha": alpha,
            "by_action": {
                action: _count_wire(target_counts[action], TARGET_ROLES)
                for action in ACTION_KEYS
            },
        },
        "training_summary": {
            "episode_count": episode_count,
            "decision_count": decision_count,
            "delay_sample_count": delay_global.counts.total(),
            "action_count": _count_wire(action_counts, ACTION_KEYS),
            "source_audit": deepcopy(dict(source_audit or {})),
        },
        "claim_boundary": {
            "simulator_executable": True,
            "comparison_ready": False,
            "voting_eligible": False,
            "deployment_allowed": False,
            "top_player_policy": False,
        },
    }
    validate_model_v1(model)
    return model


def _transition_by_trace_index(player: Mapping[str, Any]) -> dict[int, Mapping[str, Any]]:
    result: dict[int, Mapping[str, Any]] = {}
    for raw in policy_v2._array(player.get("prefix_transitions"), "prefix_transitions"):
        transition = policy_v2._mapping(raw, "prefix transition")
        trace_index = policy_v2._nonnegative_integer(
            transition.get("trace_index"), "trace_index"
        )
        if trace_index in result:
            raise HistoricalBehaviorCloneV1Error(
                "duplicate prefix transition trace_index"
            )
        result[trace_index] = transition
    return result


def training_episodes_from_stage5_v1(
    waves: Iterable[Mapping[str, Any]],
) -> tuple[list[list[TrainingDecisionV1]], dict[str, int]]:
    """Extract pooled clean-Fury episodes from already-open Stage5 wave rows."""

    episodes: list[list[TrainingDecisionV1]] = []
    audit: Counter[str] = Counter()
    for wave in waves:
        identity = policy_v2._mapping(wave.get("wave"), "wave identity")
        instance_ref = policy_v2._text(identity.get("instance_id"), "instance_id")
        encounter_id = policy_v2._text(identity.get("encounter_id"), "encounter_id")
        trace = [
            policy_v2._mapping(row, "trace row")
            for row in policy_v2._array(wave.get("exact_trace"), "exact_trace")
        ]
        audit["waves_inspected"] += 1
        for raw_player in policy_v2._array(wave.get("players"), "players"):
            player = policy_v2._mapping(raw_player, "wave player")
            metadata = policy_v2._mapping(player.get("player"), "player metadata")
            spec = policy_v2._mapping(
                player.get("warrior_spec_lane"), "warrior_spec_lane"
            )
            eligibility = policy_v2._mapping(
                player.get("eligibility_observation"), "eligibility_observation"
            )
            if (
                str(metadata.get("class") or "").upper() != "WARRIOR"
                or spec.get("partition_key") != policy_v2.FURY_LANE
                or eligibility.get("historical_fury_candidate_filter_passed") is not True
            ):
                audit["nontraining_or_nonfury_episode_excluded"] += 1
                continue
            diagnostic = policy_v4.process_player_transitions(
                player=player,
                trace=trace,
                instance_ref=instance_ref,
                encounter_id=encounter_id,
            )
            transitions = _transition_by_trace_index(player)
            episode: list[TrainingDecisionV1] = []
            for decision in diagnostic.decisions:
                if not decision.voting_usable:
                    audit["unsupported_target_decision_excluded"] += 1
                    continue
                transition = transitions[decision.trace_index]
                state = policy_v2._mapping(
                    transition.get("state_before"), "state_before"
                )
                elapsed_ms = policy_v2._nonnegative_integer(
                    state.get("wave_elapsed_ms"), "wave_elapsed_ms"
                )
                label = json.loads(decision.action_label)
                episode.append(
                    TrainingDecisionV1(
                        elapsed_ms=elapsed_ms,
                        action_key=decision.action_key,
                        target_role=label["target_role"],
                        feature_atoms=decision.feature_view.prefix_atoms,
                    )
                )
            audit.update(diagnostic.audit)
            if episode:
                episodes.append(episode)
                audit["training_episodes_retained"] += 1
                audit["strict_training_decisions_retained"] += len(episode)
            else:
                audit["empty_training_episodes_excluded"] += 1
    return episodes, dict(sorted(audit.items()))


def compile_pooled_clean_fury_stage5_v1(
    waves: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Compile actual Stage5 rows without reading raw Chronicle streams."""

    episodes, audit = training_episodes_from_stage5_v1(waves)
    return compile_training_episodes_v1(episodes, source_audit=audit)


def validate_model_v1(model: Mapping[str, Any]) -> None:
    if model.get("schema") != MODEL_SCHEMA or model.get("status") != STATUS:
        raise HistoricalBehaviorCloneV1Error("unsupported behavior-clone model")
    cohort = model.get("cohort_contract")
    if not isinstance(cohort, Mapping) or (
        cohort.get("cohort_id") != COHORT_ID
        or cohort.get("top_player_selection_used") is not False
        or cohort.get("historical_dps_ranking_used") is not False
    ):
        raise HistoricalBehaviorCloneV1Error(
            "model does not preserve the pooled-clean-Fury cohort boundary"
        )
    ontology = model.get("action_ontology")
    if not isinstance(ontology, list) or [
        row.get("action_key") for row in ontology if isinstance(row, Mapping)
    ] != list(ACTION_KEYS):
        raise HistoricalBehaviorCloneV1Error("model action ontology differs from V3")
    mark = model.get("mark_head")
    delay = model.get("delay_head")
    target = model.get("target_head")
    if not all(isinstance(value, Mapping) for value in (mark, delay, target)):
        raise HistoricalBehaviorCloneV1Error("model heads must be objects")
    counts = mark.get("global_counts")
    if not isinstance(counts, Mapping) or set(counts) != set(ACTION_KEYS):
        raise HistoricalBehaviorCloneV1Error("mark global counts do not close")
    if sum(_nonnegative_int(counts[key], f"count[{key}]") for key in ACTION_KEYS) <= 0:
        raise HistoricalBehaviorCloneV1Error("mark model is empty")
    global_delay = delay.get("global")
    if not isinstance(global_delay, Mapping) or _nonnegative_int(
        global_delay.get("sample_count"), "delay sample_count"
    ) <= 0:
        raise HistoricalBehaviorCloneV1Error("delay model is empty")


def runtime_feature_atoms_v1(
    observation: Mapping[str, Any],
) -> tuple[policy_v4.FeatureAtom, ...]:
    """Project a live simulator observation into the V4 prefix feature families."""

    if observation.get("schema") != OBSERVATION_SCHEMA:
        raise HistoricalBehaviorCloneV1Error("unsupported runtime observation schema")
    elapsed = _nonnegative_int(observation.get("wave_elapsed_ms"), "wave_elapsed_ms")
    target_count = _nonnegative_int(
        observation.get("observed_target_count"), "observed_target_count"
    )
    dead_count = _nonnegative_int(
        observation.get("observed_dead_target_count"), "observed_dead_target_count"
    )
    if dead_count > target_count:
        raise HistoricalBehaviorCloneV1Error("dead target count exceeds target count")
    background_dps = _finite_nonnegative(
        observation.get("background_dps"), "background_dps"
    )
    last_action = observation.get("last_controllable_action")
    if last_action is None:
        last_action = "NONE"
    elif last_action not in ACTION_SPEC_BY_KEY:
        raise HistoricalBehaviorCloneV1Error("last action is outside the ontology")
    last_lane = observation.get("last_controllable_lane")
    expected_lane = (
        "NONE" if last_action == "NONE" else ACTION_SPEC_BY_KEY[last_action].lane
    )
    if last_lane == "swing_queue" and expected_lane == "queue":
        last_lane = "queue"
    if last_lane is None:
        last_lane = expected_lane
    elif last_lane != expected_lane:
        raise HistoricalBehaviorCloneV1Error(
            "last action lane differs from the V3 ontology"
        )
    age = observation.get("last_gcd_action_age_ms")
    age_bucket = (
        "NEVER_OBSERVED"
        if age is None
        else policy_v2._elapsed_bucket(_nonnegative_int(age, "last_gcd_action_age_ms"))
    )
    stance = observation.get("observed_stance")
    if stance is None:
        stance = "UNKNOWN_BEFORE_OBSERVED_STANCE_ACTION"
    elif stance in STANCE_ACTION_KEYS:
        stance = str(stance).removeprefix("warrior.")
    elif stance in ACTION_SPEC_BY_KEY:
        raise HistoricalBehaviorCloneV1Error("observed stance is not a stance action")
    elif stance not in {"battle_stance", "defensive_stance", "berserker_stance"}:
        raise HistoricalBehaviorCloneV1Error("unsupported observed stance")
    coarse = {
        "wave_elapsed_bucket": policy_v2._elapsed_bucket(elapsed),
        "observed_target_count_bucket": policy_v2._count_bucket(target_count),
        "observed_dead_target_count_bucket": policy_v2._count_bucket(dead_count),
    }
    atoms = [
        policy_v4.FeatureAtom("base.wave_coarse", _canonical_text(coarse)),
        policy_v4.FeatureAtom(
            "team.background_dps_bucket", policy_v2._log_bucket(background_dps)
        ),
        policy_v4.FeatureAtom(
            "target.has_last_observed_target",
            "YES" if observation.get("actor_has_last_target") is True else "NO",
        ),
        policy_v4.FeatureAtom("sequence.last_controllable_action", str(last_action)),
        policy_v4.FeatureAtom("sequence.last_controllable_lane", str(last_lane)),
        policy_v4.FeatureAtom("sequence.last_gcd_action_age", age_bucket),
        policy_v4.FeatureAtom("sequence.observed_stance", str(stance)),
    ]
    queue = observation.get("pending_queue_action")
    if queue is None:
        atoms.append(
            policy_v4.FeatureAtom(
                "queue.prefix_observation", "NO_PENDING_QUEUE_START_OBSERVED"
            )
        )
    else:
        if queue not in {"warrior.heroic_strike", "warrior.cleave"}:
            raise HistoricalBehaviorCloneV1Error("unsupported pending queue action")
        atoms.append(policy_v4.FeatureAtom("queue.prefix_observation", queue))
        queue_age = _nonnegative_int(
            observation.get("pending_queue_age_ms"), "pending_queue_age_ms"
        )
        atoms.append(
            policy_v4.FeatureAtom(
                "queue.prefix_pending_age", policy_v2._elapsed_bucket(queue_age)
            )
        )
    return tuple(atoms)


def _posterior_mark_distribution(
    model: Mapping[str, Any], atoms: Sequence[policy_v4.FeatureAtom]
) -> tuple[dict[str, float], tuple[str, ...]]:
    mark = model["mark_head"]
    alpha = float(mark["alpha"])
    strength = float(mark["backoff_strength"])
    global_counts = mark["global_counts"]
    total = sum(int(global_counts[key]) for key in ACTION_KEYS)
    denominator = total + alpha * len(ACTION_KEYS)
    probabilities = {
        key: (int(global_counts[key]) + alpha) / denominator for key in ACTION_KEYS
    }
    matched: list[str] = []
    contexts = mark["context_counts"]
    for atom in atoms:
        family = contexts.get(atom.family)
        counts = family.get(atom.value) if isinstance(family, Mapping) else None
        if not isinstance(counts, Mapping):
            continue
        context_total = sum(int(counts[key]) for key in ACTION_KEYS)
        if context_total <= 0:
            continue
        probabilities = {
            key: (int(counts[key]) + strength * probabilities[key])
            / (context_total + strength)
            for key in ACTION_KEYS
        }
        matched.append(atom.family)
    return probabilities, tuple(matched)


def predict_mark_distribution_v1(
    model: Mapping[str, Any],
    observation: Mapping[str, Any],
    legal_actions: Iterable[str],
) -> dict[str, Any]:
    """Return the categorical mark distribution conditioned on legal actions."""

    validate_model_v1(model)
    legal = set(legal_actions)
    unknown = legal.difference(ACTION_SPEC_BY_KEY)
    if unknown:
        raise HistoricalBehaviorCloneV1Error(
            f"legal mask contains actions outside the ontology: {sorted(unknown)}"
        )
    probabilities, matched = _posterior_mark_distribution(
        model, runtime_feature_atoms_v1(observation)
    )
    mass = sum(probabilities[key] for key in ACTION_KEYS if key in legal)
    conditioned = (
        {}
        if mass <= 0
        else {
            key: probabilities[key] / mass
            for key in ACTION_KEYS
            if key in legal
        }
    )
    return {
        "probabilities": conditioned,
        "matched_context_families": list(matched),
        "legal_action_count": len(legal),
        "conditioning_applied_before_sampling": True,
    }


def _draw(distribution: Mapping[str, float], rng: random.Random) -> str:
    draw = rng.random()
    cumulative = 0.0
    last: str | None = None
    for key, probability in distribution.items():
        last = key
        cumulative += probability
        if draw < cumulative:
            return key
    if last is None:
        raise HistoricalBehaviorCloneV1Error("cannot sample an empty distribution")
    return last


def _delay_rows(value: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    rows = value.get("buckets")
    if not isinstance(rows, list):
        raise HistoricalBehaviorCloneV1Error("delay buckets must be an array")
    result: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping) or row.get("bucket") not in _delay_bucket_order():
            raise HistoricalBehaviorCloneV1Error("invalid delay bucket row")
        result[str(row["bucket"])] = row
    return result


def predict_delay_distribution_v1(
    model: Mapping[str, Any], previous_action_key: str | None
) -> dict[str, dict[str, float | int]]:
    """Return a previous-mark conditional delay posterior."""

    validate_model_v1(model)
    previous = START_ACTION if previous_action_key is None else previous_action_key
    if previous != START_ACTION and previous not in ACTION_SPEC_BY_KEY:
        raise HistoricalBehaviorCloneV1Error("previous action is outside the ontology")
    head = model["delay_head"]
    global_rows = _delay_rows(head["global"])
    local_wire = head["by_previous_action"].get(previous)
    local_rows = _delay_rows(local_wire) if isinstance(local_wire, Mapping) else {}
    global_total = sum(int(row["count"]) for row in global_rows.values())
    local_total = sum(int(row["count"]) for row in local_rows.values())
    strength = _finite_nonnegative(
        head.get("backoff_strength"), "delay backoff_strength"
    )
    if strength == 0:
        raise HistoricalBehaviorCloneV1Error(
            "delay backoff_strength must be strictly positive"
        )
    result: dict[str, dict[str, float | int]] = {}
    for bucket in _delay_bucket_order():
        global_row = global_rows.get(bucket)
        local_row = local_rows.get(bucket)
        global_probability = (
            0.0 if global_row is None else int(global_row["count"]) / global_total
        )
        probability = (
            global_probability
            if local_total == 0
            else (
                (0 if local_row is None else int(local_row["count"]))
                + strength * global_probability
            )
            / (local_total + strength)
        )
        if probability <= 0:
            continue
        representative_row = local_row or global_row
        assert representative_row is not None
        result[bucket] = {
            "probability": probability,
            "representative_ms": int(representative_row["representative_ms"]),
        }
    return result


def sample_delay_ms_v1(
    model: Mapping[str, Any],
    previous_action_key: str | None,
    rng: random.Random,
) -> int:
    rows = predict_delay_distribution_v1(model, previous_action_key)
    bucket = _draw({key: float(row["probability"]) for key, row in rows.items()}, rng)
    return int(rows[bucket]["representative_ms"])


def _target_distribution(model: Mapping[str, Any], action_key: str) -> dict[str, float]:
    target = model["target_head"]
    alpha = float(target["alpha"])
    counts = target["by_action"][action_key]
    total = sum(int(counts[key]) for key in TARGET_ROLES)
    denominator = total + alpha * len(TARGET_ROLES)
    return {key: (int(counts[key]) + alpha) / denominator for key in TARGET_ROLES}


@dataclass
class HistoricalBehaviorCloneRuntimeV1:
    """Stateful next-intent clock with explicit proposal/acceptance separation."""

    model: Mapping[str, Any]
    seed: int
    next_action_at_ms: int = field(init=False)
    last_action_key: str | None = field(init=False, default=None)
    _proposal_ordinal: int = field(init=False, default=0)
    _pending: dict[str, Any] | None = field(init=False, default=None)
    _rng: random.Random = field(init=False, repr=False)

    def __post_init__(self) -> None:
        validate_model_v1(self.model)
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise HistoricalBehaviorCloneV1Error("runtime seed must be an integer")
        self._rng = random.Random(self.seed)
        self.reset(0)

    def reset(self, start_ms: int = 0) -> None:
        start = _nonnegative_int(start_ms, "runtime start_ms")
        self.last_action_key = None
        self._proposal_ordinal = 0
        self._pending = None
        self.next_action_at_ms = start + sample_delay_ms_v1(
            self.model, None, self._rng
        )

    def propose(
        self,
        *,
        now_ms: int,
        observation: Mapping[str, Any],
        legal_actions: Iterable[str],
    ) -> dict[str, Any]:
        now = _nonnegative_int(now_ms, "runtime now_ms")
        if self._pending is not None:
            return deepcopy(self._pending)
        if now < self.next_action_at_ms:
            return {
                "kind": "WAIT",
                "wait_ms": self.next_action_at_ms - now,
                "next_action_at_ms": self.next_action_at_ms,
                "reason": "SEMI_MARKOV_CLOCK_NOT_DUE",
            }
        predicted = predict_mark_distribution_v1(
            self.model, observation, legal_actions
        )
        probabilities = predicted["probabilities"]
        if not probabilities:
            return {
                "kind": "WAIT",
                "wait_ms": 1,
                "next_action_at_ms": now + 1,
                "reason": "NO_LEGAL_ONTOLOGY_ACTION",
            }
        action_key = _draw(probabilities, self._rng)
        target_role = _draw(_target_distribution(self.model, action_key), self._rng)
        spec = ACTION_SPEC_BY_KEY[action_key]
        proposal = {
            "kind": "ACTION",
            "proposal_id": self._proposal_ordinal,
            "action_key": action_key,
            "lane": spec.lane,
            "target_role": target_role,
            "conditioned_probability": probabilities[action_key],
            "matched_context_families": predicted["matched_context_families"],
        }
        self._proposal_ordinal += 1
        self._pending = proposal
        return deepcopy(proposal)

    def record_submission(
        self, *, proposal_id: int, accepted: bool, now_ms: int
    ) -> None:
        now = _nonnegative_int(now_ms, "submission now_ms")
        if self._pending is None or self._pending.get("proposal_id") != proposal_id:
            raise HistoricalBehaviorCloneV1Error(
                "submission does not match the pending proposal"
            )
        if not isinstance(accepted, bool):
            raise HistoricalBehaviorCloneV1Error("accepted must be boolean")
        action_key = str(self._pending["action_key"])
        self._pending = None
        if not accepted:
            self.next_action_at_ms = now
            return
        self.last_action_key = action_key
        self.next_action_at_ms = now + sample_delay_ms_v1(
            self.model, action_key, self._rng
        )


__all__ = [
    "ACTION_KEYS",
    "COHORT_ID",
    "HistoricalBehaviorCloneRuntimeV1",
    "HistoricalBehaviorCloneV1Error",
    "MODEL_SCHEMA",
    "OBSERVATION_SCHEMA",
    "STATUS",
    "TARGET_ROLES",
    "TrainingDecisionV1",
    "compile_pooled_clean_fury_stage5_v1",
    "compile_training_episodes_v1",
    "predict_delay_distribution_v1",
    "predict_mark_distribution_v1",
    "runtime_feature_atoms_v1",
    "sample_delay_ms_v1",
    "training_episodes_from_stage5_v1",
    "validate_model_v1",
]
