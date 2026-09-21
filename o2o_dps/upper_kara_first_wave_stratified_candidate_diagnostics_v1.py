"""Post-hoc first-wave-arrival diagnostics for a completed remote campaign.

The training campaign predeclares ``first_wave_arrival_ms`` and uses
occurrence parity *within each arrival stratum* to separate proposal examples
from selection-validation examples.  This reducer preserves that split and
reports, for every searched program and every arrival stratum:

* proposal-cohort paired mean damage versus the exact paired Cat zero arm;
* selection-validation paired mean, normal 95% interval, and win/tie/loss;
* the result of choosing the stratum's proposal champion and applying the
  independent positive-lower-bound validation gate.

``first_wave_arrival_ms`` is control-plane configuration, not a field in the
causal policy observation.  Therefore the result is diagnostic evidence only:
it may motivate a search for a prefix-observable guard, but it is not a legal
arrival-oracle routing table.

The reducer reads one v1 training terminal at a time and retains only running
paired moments.  It is intended to run beside the remote artifacts; the large
training terminal set does not need to be copied to the local workstation.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
from statistics import NormalDist
import tempfile
from typing import Any, Iterable, Mapping, Sequence

from .causal_action_program_v1 import (
    ProgramOriginV1,
    causal_action_program_from_dict_v1,
)
from .upper_kara_causal_program_remote_contract_v1 import (
    FixedParentQueueGcdBlockSearchV1,
    TERMINAL_COMPLETE,
    TrainingShardV1,
    assign_training_shards_v1,
    load_continuous_two_wave_remote_campaign_v1,
    split_training_examples_for_selection_v1,
)
from .upper_kara_causal_program_remote_worker_v1 import (
    SUMMARY_TERMINAL_SCHEMA,
    TRAIN_TERMINAL_SCHEMA,
    _campaign_contract_v1,
    train_terminal_name_v1,
)
from .upper_kara_paired_cat_selection_v1 import (
    exact_cat_zero_residual_ref_v1,
    exact_paired_zero_program_ref_v1,
)


JSONMap = dict[str, Any]
SCHEMA = "upper_kara_first_wave_stratified_candidate_diagnostics/v1"
DEFAULT_CONFIDENCE_LEVEL = 0.95
DEFAULT_MINIMUM_VALIDATION_PAIRS_PER_STRATUM = 2
DEFAULT_EXPECTED_SEARCHED_PROGRAM_COUNT = 309


def _read_json(path: str | Path, label: str) -> JSONMap:
    source = Path(path).expanduser().resolve(strict=True)
    try:
        value = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read {label}: {error}") from error
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    return dict(value)


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _atomic_create_json(path: str | Path, value: Mapping[str, Any]) -> None:
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number")
    return result


@dataclass
class _PairedMoments:
    """Numerically stable streaming paired-delta summary."""

    count: int = 0
    mean: float = 0.0
    m2: float = 0.0
    wins: int = 0
    ties: int = 0
    losses: int = 0

    def add(self, value: float) -> None:
        value = _finite(value, "paired damage delta")
        self.count += 1
        offset = value - self.mean
        self.mean += offset / self.count
        self.m2 += offset * (value - self.mean)
        if value > 0.0:
            self.wins += 1
        elif value < 0.0:
            self.losses += 1
        else:
            self.ties += 1

    def proposal_wire(self) -> JSONMap:
        if self.count <= 0:
            raise ValueError("proposal cohort is empty")
        return {
            "pair_count": self.count,
            "mean_paired_damage_delta": self.mean,
        }

    def validation_wire(self, confidence_level: float) -> JSONMap:
        if self.count <= 0:
            raise ValueError("selection-validation cohort is empty")
        sample_standard_deviation: float | None = None
        standard_error: float | None = None
        interval: JSONMap | None = None
        critical = NormalDist().inv_cdf(0.5 + confidence_level / 2.0)
        if self.count >= 2:
            variance = max(0.0, self.m2 / (self.count - 1))
            sample_standard_deviation = math.sqrt(variance)
            standard_error = sample_standard_deviation / math.sqrt(self.count)
            interval = {
                "lower_bound": self.mean - critical * standard_error,
                "upper_bound": self.mean + critical * standard_error,
            }
        return {
            "pair_count": self.count,
            "mean_paired_damage_delta": self.mean,
            "sample_standard_deviation": sample_standard_deviation,
            "standard_error": standard_error,
            "confidence_level": confidence_level,
            "normal_critical_value": critical,
            "normal_confidence_interval": interval,
            "win_tie_loss": {
                "wins": self.wins,
                "ties": self.ties,
                "losses": self.losses,
            },
        }


@dataclass(frozen=True)
class _CandidateIdentity:
    index: int
    program_ref: str
    program_id: str
    program_key: str
    family: str
    is_zero: bool

    def registry_wire(self) -> JSONMap:
        # program_key is deliberately verified but omitted: for these programs
        # it is the complete multi-kilobyte serialized object, not a compact ID.
        return {
            "candidate_index": self.index,
            "program_ref": self.program_ref,
            "program_id": self.program_id,
            "family": self.family,
            "is_zero": self.is_zero,
        }


def _candidate_family(receipt: Mapping[str, Any], *, is_zero: bool) -> str:
    if is_zero:
        return "PAIRED_CAT_ZERO"
    source_refs = receipt.get("program", {}).get("source_refs")
    if isinstance(source_refs, list):
        markers = [
            value.split(":", 1)[1]
            for value in source_refs
            if isinstance(value, str) and value.startswith("sparse-family:")
        ]
        if len(markers) == 1 and markers[0]:
            return markers[0]
    return "UNCLASSIFIED_SEARCHED"


def _resolve_candidates(
    campaign: Any,
    loadout_id: str,
    raw_programs: object,
    *,
    expected_searched_program_count: int | None,
) -> tuple[tuple[_CandidateIdentity, ...], str]:
    if not isinstance(raw_programs, list) or not raw_programs:
        raise ValueError("training terminal has no program receipts")
    receipts: list[JSONMap] = []
    for raw in raw_programs:
        if not isinstance(raw, Mapping):
            raise ValueError("training program receipt must be an object")
        row = dict(raw)
        if (
            not isinstance(row.get("program_ref"), str)
            or not isinstance(row.get("program_id"), str)
            or row["program_ref"] != row["program_id"]
            or not isinstance(row.get("program_key"), str)
            or not isinstance(row.get("program"), Mapping)
        ):
            raise ValueError("training program receipt identity is malformed")
        receipts.append(row)
    searched = [
        row
        for row in receipts
        if row.get("program_origin") == ProgramOriginV1.SEARCHED.value
    ]
    for row in searched:
        try:
            program = causal_action_program_from_dict_v1(row["program"])
        except (TypeError, ValueError) as error:
            raise ValueError("searched program receipt cannot be decoded") from error
        if (
            row["program_ref"] != program.program_id
            or row["program_id"] != program.program_id
            or row["program_key"] != program.program_key()
            or row["program_origin"] != program.origin.value
        ):
            raise ValueError("searched program receipt identity mismatch")
    if (
        expected_searched_program_count is not None
        and len(searched) != expected_searched_program_count
    ):
        raise ValueError(
            "searched program count differs from the frozen diagnostic "
            f"expectation: expected {expected_searched_program_count}, "
            f"observed {len(searched)}"
        )
    refs = [row["program_ref"] for row in searched]
    keys = [row["program_key"] for row in searched]
    if not searched or len(refs) != len(set(refs)) or len(keys) != len(set(keys)):
        raise ValueError("searched program identities must be nonempty and unique")

    if isinstance(campaign.search_spec, FixedParentQueueGcdBlockSearchV1):
        zero_ref = exact_paired_zero_program_ref_v1(
            campaign.search_spec.parent_program,
            searched,
            label=f"{loadout_id} fixed-parent paired Cat zero",
        )
    else:
        zero_ref = exact_cat_zero_residual_ref_v1(loadout_id, searched)
    candidates = tuple(
        _CandidateIdentity(
            index=index,
            program_ref=row["program_ref"],
            program_id=row["program_id"],
            program_key=row["program_key"],
            family=_candidate_family(row, is_zero=row["program_ref"] == zero_ref),
            is_zero=row["program_ref"] == zero_ref,
        )
        for index, row in enumerate(searched)
    )
    if sum(row.is_zero for row in candidates) != 1:
        raise ValueError("searched family must contain exactly one paired Cat zero")
    return candidates, zero_ref


def _searched_signature(raw_programs: object) -> tuple[tuple[str, str, str], ...]:
    if not isinstance(raw_programs, list):
        raise ValueError("training terminal programs must be a list")
    result: list[tuple[str, str, str]] = []
    for raw in raw_programs:
        if not isinstance(raw, Mapping):
            raise ValueError("training program receipt must be an object")
        if raw.get("program_origin") != ProgramOriginV1.SEARCHED.value:
            continue
        ref = raw.get("program_ref")
        key = raw.get("program_key")
        origin = raw.get("program_origin")
        if not all(isinstance(value, str) and value for value in (ref, key, origin)):
            raise ValueError("searched program signature is malformed")
        result.append((ref, key, origin))
    return tuple(result)


def _heldout_projection(
    heldout: Mapping[str, Any], campaign: Any, candidate_refs: set[str]
) -> JSONMap:
    if (
        heldout.get("schema") != SUMMARY_TERMINAL_SCHEMA
        or heldout.get("terminal_status") != TERMINAL_COMPLETE
        or heldout.get("campaign_id") != campaign.campaign_id
        or heldout.get("build_id") != campaign.build_id
        or heldout.get("campaign_contract") != _campaign_contract_v1(campaign)
    ):
        raise ValueError("heldout summary is not a complete matching campaign artifact")
    loadout_id = heldout.get("selected_loadout_id")
    if loadout_id not in campaign.loadout_ids:
        raise ValueError("heldout summary selected loadout is outside the campaign")
    frozen_ref = heldout.get("frozen_program_id")
    if frozen_ref not in candidate_refs:
        raise ValueError("heldout frozen program is absent from the searched family")
    baseline_ids = heldout.get("baseline_policy_ids")
    metric = heldout.get("metric")
    if (
        not isinstance(baseline_ids, list)
        or not baseline_ids
        or any(not isinstance(value, str) or not value for value in baseline_ids)
        or len(baseline_ids) != len(set(baseline_ids))
        or not isinstance(metric, Mapping)
        or not isinstance(metric.get("by_first_wave_arrival_ms"), list)
    ):
        raise ValueError("heldout summary lacks stratified metrics")
    expected_arrivals = {
        row.first_wave_arrival_ms for row in campaign.evaluation_examples
    }
    rows: list[JSONMap] = []
    observed_arrivals: set[int] = set()
    for raw in metric["by_first_wave_arrival_ms"]:
        if not isinstance(raw, Mapping):
            raise ValueError("heldout arrival metric must be an object")
        arrival = raw.get("first_wave_arrival_ms")
        if isinstance(arrival, bool) or not isinstance(arrival, int):
            raise ValueError("heldout arrival metric has invalid arrival")
        if arrival in observed_arrivals:
            raise ValueError("heldout summary repeats an arrival stratum")
        observed_arrivals.add(arrival)
        seed_count = raw.get("seed_count")
        if (
            isinstance(seed_count, bool)
            or not isinstance(seed_count, int)
            or seed_count < 1
        ):
            raise ValueError("heldout arrival metric has invalid seed_count")
        policy_means = raw.get("policy_means")
        paired = raw.get("paired_candidate_minus_baseline_damage_statistics")
        if not isinstance(policy_means, Mapping) or not isinstance(paired, Mapping):
            raise ValueError("heldout arrival metric lacks paired panel statistics")
        mean_damage: JSONMap = {}
        for policy_id in ("frozen_candidate", *baseline_ids):
            policy_row = policy_means.get(policy_id)
            if not isinstance(policy_row, Mapping):
                raise ValueError("heldout arrival metric lacks a policy mean")
            mean_damage[policy_id] = _finite(
                policy_row.get("mean_own_effective_damage"),
                "heldout mean_own_effective_damage",
            )
        paired_projection: JSONMap = {}
        for policy_id in baseline_ids:
            statistic = paired.get(policy_id)
            if not isinstance(statistic, Mapping):
                raise ValueError("heldout arrival metric lacks a paired statistic")
            pair_count = statistic.get("pair_count")
            if (
                isinstance(pair_count, bool)
                or not isinstance(pair_count, int)
                or pair_count < 1
            ):
                raise ValueError("heldout paired statistic has invalid pair_count")
            mean_delta = _finite(
                statistic.get("mean_damage_delta"),
                "heldout paired mean_damage_delta",
            )
            interval = statistic.get("normal_confidence_interval")
            if not isinstance(interval, Mapping):
                raise ValueError("heldout paired statistic lacks its interval")
            projected_interval = {
                "lower_bound": _finite(
                    interval.get("lower_bound"), "heldout paired lower_bound"
                ),
                "upper_bound": _finite(
                    interval.get("upper_bound"), "heldout paired upper_bound"
                ),
            }
            win_tie_loss = statistic.get("win_tie_loss")
            if (
                not isinstance(win_tie_loss, Mapping)
                or set(win_tie_loss) != {"wins", "ties", "losses"}
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value < 0
                    for value in win_tie_loss.values()
                )
                or sum(win_tie_loss.values()) != pair_count
                or pair_count != seed_count
            ):
                raise ValueError("heldout paired statistic has invalid win/tie/loss")
            paired_projection[policy_id] = {
                "pair_count": pair_count,
                "mean_damage_delta": mean_delta,
                "normal_confidence_interval": projected_interval,
                "win_tie_loss": dict(win_tie_loss),
            }
        rows.append(
            {
                "first_wave_arrival_ms": arrival,
                "seed_count": seed_count,
                "mean_own_effective_damage": mean_damage,
                "paired_frozen_candidate_minus_baseline": paired_projection,
            }
        )
    if observed_arrivals != expected_arrivals:
        raise ValueError("heldout summary arrival strata differ from the campaign")
    return {
        "selected_loadout_id": loadout_id,
        "frozen_program_ref": frozen_ref,
        "baseline_policy_ids": list(baseline_ids),
        "by_first_wave_arrival_ms": sorted(
            rows, key=lambda row: row["first_wave_arrival_ms"]
        ),
        "scope": (
            "FINAL_HELDOUT_REPLAY_OF_ONE_GLOBALLY_FROZEN_PROGRAM;_"
            "DOES_NOT_VALIDATE_UNREPLAYED_STRATUM_CHAMPIONS"
        ),
    }


def reduce_first_wave_stratified_candidate_diagnostics_v1(
    *,
    campaign: Any,
    shard_terminals: Iterable[tuple[TrainingShardV1, Mapping[str, Any]]],
    heldout_summary: Mapping[str, Any],
    expected_searched_program_count: int | None = (
        DEFAULT_EXPECTED_SEARCHED_PROGRAM_COUNT
    ),
    confidence_level: float = DEFAULT_CONFIDENCE_LEVEL,
    minimum_validation_pairs_per_stratum: int = (
        DEFAULT_MINIMUM_VALIDATION_PAIRS_PER_STRATUM
    ),
) -> JSONMap:
    """Reduce validated v1 training terminals into compact stratum evidence."""

    if not 0.5 < confidence_level < 1.0:
        raise ValueError("confidence_level must be between 0.5 and 1")
    if (
        isinstance(minimum_validation_pairs_per_stratum, bool)
        or not isinstance(minimum_validation_pairs_per_stratum, int)
        or minimum_validation_pairs_per_stratum < 2
    ):
        raise ValueError("minimum_validation_pairs_per_stratum must be >= 2")
    if expected_searched_program_count is not None and (
        isinstance(expected_searched_program_count, bool)
        or not isinstance(expected_searched_program_count, int)
        or expected_searched_program_count < 1
    ):
        raise ValueError("expected_searched_program_count must be positive or null")

    proposal_examples, validation_examples = (
        split_training_examples_for_selection_v1(campaign)
    )
    cohort_by_seed = {
        **{row.seed: "PROPOSAL" for row in proposal_examples},
        **{row.seed: "SELECTION_VALIDATION" for row in validation_examples},
    }
    arrival_by_seed = {
        row.seed: row.first_wave_arrival_ms for row in campaign.train_examples
    }
    expected_counts = Counter(
        (arrival_by_seed[seed], cohort) for seed, cohort in cohort_by_seed.items()
    )
    expected_shards = {
        (row.loadout_id, row.seed_shard_index): row
        for row in assign_training_shards_v1(campaign)
    }
    observed_shards: set[tuple[str, int]] = set()
    candidates_by_loadout: dict[str, tuple[_CandidateIdentity, ...]] = {}
    zero_by_loadout: dict[str, str] = {}
    signature_by_loadout: dict[str, tuple[tuple[str, str, str], ...]] = {}
    moments: dict[tuple[str, int, str, str], _PairedMoments] = {}

    for shard, terminal_value in shard_terminals:
        shard_key = (shard.loadout_id, shard.seed_shard_index)
        if expected_shards.get(shard_key) != shard:
            raise ValueError(f"unexpected training shard: {shard.work_id}")
        if shard_key in observed_shards:
            raise ValueError(f"duplicate training shard: {shard.work_id}")
        observed_shards.add(shard_key)
        terminal = dict(terminal_value)
        if (
            terminal.get("schema") != TRAIN_TERMINAL_SCHEMA
            or terminal.get("terminal_status") != TERMINAL_COMPLETE
            or terminal.get("campaign_id") != campaign.campaign_id
            or terminal.get("build_id") != campaign.build_id
            or terminal.get("campaign_contract") != _campaign_contract_v1(campaign)
            or terminal.get("loadout_id") != shard.loadout_id
            or terminal.get("seed_shard_index") != shard.seed_shard_index
            or terminal.get("examples") != [row.to_dict() for row in shard.examples]
        ):
            raise ValueError(
                f"training terminal is not a complete matching shard: {shard.work_id}"
            )
        signature = _searched_signature(terminal.get("programs"))
        if shard.loadout_id not in candidates_by_loadout:
            candidates, zero_ref = _resolve_candidates(
                campaign,
                shard.loadout_id,
                terminal.get("programs"),
                expected_searched_program_count=expected_searched_program_count,
            )
            candidates_by_loadout[shard.loadout_id] = candidates
            zero_by_loadout[shard.loadout_id] = zero_ref
            signature_by_loadout[shard.loadout_id] = signature
        elif signature != signature_by_loadout[shard.loadout_id]:
            raise ValueError(
                f"searched candidate family differs across shards: {shard.loadout_id}"
            )
        candidates = candidates_by_loadout[shard.loadout_id]
        candidate_refs = {row.program_ref for row in candidates}
        zero_ref = zero_by_loadout[shard.loadout_id]
        expected_seeds = {row.seed for row in shard.examples}
        lane_by_ref_seed: dict[tuple[str, int], Mapping[str, Any]] = {}
        raw_lanes = terminal.get("lanes")
        if not isinstance(raw_lanes, list):
            raise ValueError(f"training terminal lacks lanes: {shard.work_id}")
        for raw_lane in raw_lanes:
            if not isinstance(raw_lane, Mapping):
                raise ValueError("training lane must be an object")
            ref = raw_lane.get("program_ref")
            if ref not in candidate_refs:
                continue
            seed = raw_lane.get("master_seed")
            if seed not in expected_seeds:
                raise ValueError("searched lane refers to a seed outside its shard")
            key = (ref, seed)
            if key in lane_by_ref_seed:
                raise ValueError("training terminal repeats a searched program/seed lane")
            if raw_lane.get("status") != TERMINAL_COMPLETE:
                raise ValueError("searched diagnostics require every paired lane COMPLETE")
            if raw_lane.get("first_wave_arrival_ms") != arrival_by_seed[seed]:
                raise ValueError("training lane arrival differs from campaign binding")
            _finite(raw_lane.get("own_effective_damage"), "own_effective_damage")
            lane_by_ref_seed[key] = raw_lane
        expected_pairs = {
            (candidate.program_ref, seed)
            for candidate in candidates
            for seed in expected_seeds
        }
        if set(lane_by_ref_seed) != expected_pairs:
            raise ValueError("training terminal searched-lane coverage mismatch")
        for seed in expected_seeds:
            zero_lane = lane_by_ref_seed[(zero_ref, seed)]
            zero_damage = _finite(
                zero_lane.get("own_effective_damage"), "zero own_effective_damage"
            )
            zero_simulator_seed = zero_lane.get("simulator_seed")
            for candidate in candidates:
                lane = lane_by_ref_seed[(candidate.program_ref, seed)]
                if lane.get("simulator_seed") != zero_simulator_seed:
                    raise ValueError("paired lanes have different simulator seeds")
                delta = _finite(
                    lane.get("own_effective_damage"), "candidate own_effective_damage"
                ) - zero_damage
                key = (
                    shard.loadout_id,
                    arrival_by_seed[seed],
                    candidate.program_ref,
                    cohort_by_seed[seed],
                )
                moments.setdefault(key, _PairedMoments()).add(delta)

    if observed_shards != set(expected_shards):
        missing = sorted(set(expected_shards) - observed_shards)
        raise ValueError(f"training shard coverage is incomplete: {missing[:5]}")

    candidate_ref_set = {
        row.program_ref
        for candidates in candidates_by_loadout.values()
        for row in candidates
    }
    heldout = _heldout_projection(heldout_summary, campaign, candidate_ref_set)
    frozen_ref = heldout["frozen_program_ref"]
    strata: list[JSONMap] = []
    accepted_nonzero_count = 0
    family_counts: Counter[str] = Counter()
    candidate_registry: list[JSONMap] = []
    for loadout_id in campaign.loadout_ids:
        candidates = candidates_by_loadout[loadout_id]
        for candidate in candidates:
            family_counts[candidate.family] += 1
            candidate_registry.append(
                {"loadout_id": loadout_id, **candidate.registry_wire()}
            )
        zero_ref = zero_by_loadout[loadout_id]
        for arrival in sorted(set(arrival_by_seed.values())):
            rows: list[JSONMap] = []
            for candidate in candidates:
                proposal = moments.get(
                    (loadout_id, arrival, candidate.program_ref, "PROPOSAL")
                )
                validation = moments.get(
                    (
                        loadout_id,
                        arrival,
                        candidate.program_ref,
                        "SELECTION_VALIDATION",
                    )
                )
                expected_proposal = expected_counts[(arrival, "PROPOSAL")]
                expected_validation = expected_counts[
                    (arrival, "SELECTION_VALIDATION")
                ]
                if (
                    proposal is None
                    or validation is None
                    or proposal.count != expected_proposal
                    or validation.count != expected_validation
                ):
                    raise ValueError(
                        "candidate/cohort coverage differs within an arrival stratum"
                    )
                rows.append(
                    {
                        "candidate_index": candidate.index,
                        "program_ref": candidate.program_ref,
                        "family": candidate.family,
                        "is_zero": candidate.is_zero,
                        "proposal": proposal.proposal_wire(),
                        "selection_validation": validation.validation_wire(
                            confidence_level
                        ),
                    }
                )
            ranked = sorted(
                rows,
                key=lambda row: (
                    -row["proposal"]["mean_paired_damage_delta"],
                    not row["is_zero"],
                    row["program_ref"],
                ),
            )
            for rank, row in enumerate(ranked, start=1):
                row["proposal_rank"] = rank
            champion = ranked[0]
            validation_interval = champion["selection_validation"][
                "normal_confidence_interval"
            ]
            if champion["program_ref"] == zero_ref:
                gate_status = "PAIRED_ZERO_SELECTED_ON_PROPOSAL"
                accepted_ref = zero_ref
            elif (
                champion["selection_validation"]["pair_count"]
                < minimum_validation_pairs_per_stratum
            ):
                gate_status = "FALLBACK_PAIRED_ZERO_INSUFFICIENT_VALIDATION_PAIRS"
                accepted_ref = zero_ref
            elif (
                validation_interval is not None
                and validation_interval["lower_bound"] > 0.0
            ):
                gate_status = "NONZERO_STRATUM_CHAMPION_ACCEPTED"
                accepted_ref = champion["program_ref"]
                accepted_nonzero_count += 1
            else:
                gate_status = "FALLBACK_PAIRED_ZERO_LCB_NOT_POSITIVE"
                accepted_ref = zero_ref
            strata.append(
                {
                    "loadout_id": loadout_id,
                    "first_wave_arrival_ms": arrival,
                    "proposal_pair_count_per_candidate": expected_counts[
                        (arrival, "PROPOSAL")
                    ],
                    "selection_validation_pair_count_per_candidate": (
                        expected_counts[(arrival, "SELECTION_VALIDATION")]
                    ),
                    "proposal_champion": {
                        "program_ref": champion["program_ref"],
                        "family": champion["family"],
                        "is_zero": champion["is_zero"],
                        "mean_paired_damage_delta": champion["proposal"][
                            "mean_paired_damage_delta"
                        ],
                    },
                    "selection_validation_gate": {
                        "status": gate_status,
                        "diagnostic_accepted_program_ref": accepted_ref,
                        "accepted_is_zero": accepted_ref == zero_ref,
                        "champion_validation": champion[
                            "selection_validation"
                        ],
                    },
                    "heldout_frozen_program_is_proposal_champion": (
                        frozen_ref == champion["program_ref"]
                    ),
                    "candidates_by_proposal_rank": ranked,
                }
            )

    return {
        "schema": SCHEMA,
        "terminal_status": TERMINAL_COMPLETE,
        "campaign_id": campaign.campaign_id,
        "build_id": campaign.build_id,
        "searched_program_count_per_loadout": {
            loadout_id: len(candidates_by_loadout[loadout_id])
            for loadout_id in campaign.loadout_ids
        },
        "paired_zero_program_ref_by_loadout": dict(zero_by_loadout),
        "candidate_family_counts": dict(sorted(family_counts.items())),
        "candidate_registry": candidate_registry,
        "strata": strata,
        "gate_summary": {
            "stratum_count": len(strata),
            "accepted_nonzero_stratum_count": accepted_nonzero_count,
            "fallback_or_zero_stratum_count": len(strata)
            - accepted_nonzero_count,
        },
        "heldout_context": heldout,
        "policy_observability": {
            "field": "first_wave_arrival_ms",
            "directly_observable_by_policy": False,
            "present_in_policy_observation_causal_projection_v1": False,
            "semantics": (
                "PREDECLARED_CONTROL_PLANE_FUTURE_TARGET_INTRODUCTION_DELAY"
            ),
            "direct_arrival_stratum_routing_authorized": False,
            "permitted_use": (
                "POST_HOC_DIAGNOSTIC_TO_DISCOVER_AND_VALIDATE_A_"
                "PREFIX_OBSERVABLE_CAUSAL_GUARD"
            ),
        },
        "contract": {
            "proposal_cohort": (
                "FIRST_WAVE_ARRIVAL_STRATIFIED_EVEN_OCCURRENCES"
            ),
            "selection_validation_cohort": (
                "FIRST_WAVE_ARRIVAL_STRATIFIED_ODD_OCCURRENCES"
            ),
            "objective": "PAIRED_OWN_EFFECTIVE_DAMAGE_VS_EXACT_PAIRED_CAT_ZERO",
            "same_seed_and_simulator_seed_pairing_required": True,
            "all_searched_programs_reported": True,
            "candidate_program_keys_verified_but_omitted_for_compactness": True,
            "validation_interval": "PAIRED_MEAN_NORMAL_TWO_SIDED",
            "confidence_level": confidence_level,
            "nonzero_gate": "VALIDATION_LOWER_BOUND_STRICTLY_GT_ZERO",
            "minimum_validation_pairs_per_stratum": (
                minimum_validation_pairs_per_stratum
            ),
            "failed_lanes_scored_as_zero": False,
            "heldout_used_for_candidate_selection": False,
            "diagnostic_strata_are_not_deployable_policy_routes": True,
        },
    }


def reduce_first_wave_stratified_candidate_artifacts_v1(
    *,
    campaign_path: str | Path,
    training_root: str | Path,
    heldout_summary_path: str | Path,
    expected_searched_program_count: int | None = (
        DEFAULT_EXPECTED_SEARCHED_PROGRAM_COUNT
    ),
    confidence_level: float = DEFAULT_CONFIDENCE_LEVEL,
    minimum_validation_pairs_per_stratum: int = (
        DEFAULT_MINIMUM_VALIDATION_PAIRS_PER_STRATUM
    ),
) -> JSONMap:
    """Read remote artifacts sequentially and return one compact diagnostic."""

    campaign = load_continuous_two_wave_remote_campaign_v1(campaign_path)
    root = Path(training_root).expanduser()

    def terminals() -> Iterable[tuple[TrainingShardV1, Mapping[str, Any]]]:
        for shard in assign_training_shards_v1(campaign):
            yield shard, _read_json(
                root / train_terminal_name_v1(shard),
                f"training terminal {shard.work_id}",
            )

    return reduce_first_wave_stratified_candidate_diagnostics_v1(
        campaign=campaign,
        shard_terminals=terminals(),
        heldout_summary=_read_json(heldout_summary_path, "heldout summary"),
        expected_searched_program_count=expected_searched_program_count,
        confidence_level=confidence_level,
        minimum_validation_pairs_per_stratum=(
            minimum_validation_pairs_per_stratum
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", required=True, type=Path)
    parser.add_argument("--training-root", required=True, type=Path)
    parser.add_argument("--heldout-summary", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--expected-searched-program-count",
        type=int,
        default=DEFAULT_EXPECTED_SEARCHED_PROGRAM_COUNT,
    )
    parser.add_argument(
        "--minimum-validation-pairs-per-stratum",
        type=int,
        default=DEFAULT_MINIMUM_VALIDATION_PAIRS_PER_STRATUM,
    )
    parser.add_argument(
        "--confidence-level", type=float, default=DEFAULT_CONFIDENCE_LEVEL
    )
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists")
    result = reduce_first_wave_stratified_candidate_artifacts_v1(
        campaign_path=args.campaign,
        training_root=args.training_root,
        heldout_summary_path=args.heldout_summary,
        expected_searched_program_count=args.expected_searched_program_count,
        confidence_level=args.confidence_level,
        minimum_validation_pairs_per_stratum=(
            args.minimum_validation_pairs_per_stratum
        ),
    )
    _atomic_create_json(args.output, result)
    print(
        json.dumps(
            {
                "status": result["terminal_status"],
                "campaign_id": result["campaign_id"],
                "searched_program_count_per_loadout": result[
                    "searched_program_count_per_loadout"
                ],
                **result["gate_summary"],
                "direct_arrival_routing_authorized": result[
                    "policy_observability"
                ]["direct_arrival_stratum_routing_authorized"],
                "output": str(args.output),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()


__all__ = [
    "DEFAULT_CONFIDENCE_LEVEL",
    "DEFAULT_EXPECTED_SEARCHED_PROGRAM_COUNT",
    "DEFAULT_MINIMUM_VALIDATION_PAIRS_PER_STRATUM",
    "SCHEMA",
    "reduce_first_wave_stratified_candidate_artifacts_v1",
    "reduce_first_wave_stratified_candidate_diagnostics_v1",
]
