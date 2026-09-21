"""Development-only strict-prefix white-mark opportunities from Stage-5 rows.

The event time is known at the mark decision. The current mark/target/damage
remain labels; only earlier rows update the actor's white and START history.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Mapping

from .chronicle_external_teammate_response_model_v1 import _delay_bucket


@dataclass
class _ActorClock:
    elapsed_ms: int = 0
    event_count: int = 0
    last_white_ms: int | None = None
    prior_direct_hostile_start: bool = False


class White6603OpportunityClockV8:
    """Reset once per Stage-5 wave; consume rows in exact EventMeta order."""

    def __init__(self) -> None:
        self._clocks: dict[str, _ActorClock] = {}

    def observe(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """Compute features before the current label updates actor history."""

        actor = row["actor"]
        label = row["label"]
        guid = actor["player_guid"]
        clock = self._clocks.setdefault(guid, _ActorClock())
        origin = "WAVE_START" if clock.event_count == 0 else "PREVIOUS_ACTOR_EVENT"
        if label["delay_origin"] != origin:
            raise ValueError("white opportunity row delay origin differs from actor prefix")
        delay_ms = label["inter_event_delay_ms"]
        if type(delay_ms) is not int or delay_ms < 0:
            raise ValueError("white opportunity delay must be a nonnegative integer")
        elapsed_ms = clock.elapsed_ms + delay_ms
        phase = "FIRST" if clock.last_white_ms is None else "REPEAT"
        age_ms = (
            elapsed_ms if clock.last_white_ms is None
            else elapsed_ms - clock.last_white_ms
        )
        hero_class = actor["class"]
        spec = actor["spec_key"]
        age_bucket = _delay_bucket(age_ms)
        prior_start = int(clock.prior_direct_hostile_start)
        contexts = (
            ("CLASS_SPEC", hero_class, spec, phase, age_bucket, prior_start),
            ("CLASS", hero_class, phase, age_bucket, prior_start),
            ("CLASS_PHASE", hero_class, phase),
            ("GLOBAL", phase),
        )
        direct_player = label["attribution_kind"] == "DIRECT_FRIENDLY_PLAYER"
        direct_hostile = direct_player and label["target_lane"] == "HOSTILE_CREATURE"
        white = (
            direct_player
            and label["event_type"] == "DMG"
            and label["spell_id"] == 6603
        )
        opportunity = {
            "actor_guid": guid,
            "elapsed_ms": elapsed_ms,
            "phase": phase,
            "age_ms": age_ms,
            "age_bucket": age_bucket,
            "prior_direct_hostile_start": bool(prior_start),
            "contexts": contexts,
            "white6603": white,
        }
        clock.elapsed_ms = elapsed_ms
        clock.event_count += 1
        if direct_hostile and label["event_type"] == "START":
            clock.prior_direct_hostile_start = True
        if white:
            clock.last_white_ms = elapsed_ms
        return opportunity


def iter_white6603_opportunities_v8(
    rows: Iterable[Mapping[str, Any]],
) -> Iterator[dict[str, Any]]:
    """Yield one prefix observation and binary label per exact actor event."""

    clock = White6603OpportunityClockV8()
    for row in rows:
        yield clock.observe(row)


def count_white6603_opportunities_v8(
    rows: Iterable[Mapping[str, Any]],
) -> dict[tuple[Any, ...], Counter[bool]]:
    """Bounded-cardinality counts; no GUID or raw timestamp enters a key."""

    counts: dict[tuple[Any, ...], Counter[bool]] = {}
    for opportunity in iter_white6603_opportunities_v8(rows):
        for context in opportunity["contexts"]:
            counts.setdefault(context, Counter())[opportunity["white6603"]] += 1
    return counts


def white6603_opportunity_from_prefix_v8(row: Mapping[str, Any]) -> dict[str, Any]:
    """Read the v8 sufficient-row prefix used by the runtime, before its label."""

    actor = row["actor"]
    emission = row["emission_state_before_current_event"]
    label = row["label"]
    phase = emission["white6603_phase"]
    elapsed_ms = emission["wave_elapsed_ms"]
    age_ms = emission["white6603_age_ms"]
    prior_start = int(emission["actor_has_prior_direct_hostile_start"])
    hero_class = actor["class"]
    spec = actor["spec_key"]
    age_bucket = _delay_bucket(age_ms)
    return {
        "actor_guid": actor["player_guid"],
        "elapsed_ms": elapsed_ms,
        "phase": phase,
        "age_ms": age_ms,
        "age_bucket": age_bucket,
        "prior_direct_hostile_start": bool(prior_start),
        "contexts": (
            ("CLASS_SPEC", hero_class, spec, phase, age_bucket, prior_start),
            ("CLASS", hero_class, phase, age_bucket, prior_start),
            ("CLASS_PHASE", hero_class, phase),
            ("GLOBAL", phase),
        ),
        "white6603": (
            label["attribution_kind"] == "DIRECT_FRIENDLY_PLAYER"
            and label["event_type"] == "DMG"
            and label["spell_id"] == 6603
        ),
    }
