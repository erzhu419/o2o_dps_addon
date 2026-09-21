"""Development-only white6603 opportunity head over strict-prefix counts."""

from __future__ import annotations

from collections import Counter
from typing import Any, Mapping

from .chronicle_external_teammate_response_model_v1 import _delay_bucket


class DevelopmentWhite6603CountHeadV8:
    def __init__(
        self,
        counts: Mapping[tuple[Any, ...], Counter[bool]],
        *,
        minimums: Mapping[str, int],
        alpha: float = 1.0,
    ) -> None:
        if alpha not in (0.5, 1.0):
            raise ValueError("white6603 alpha must be one of the frozen candidates: 0.5, 1")
        self.counts = counts
        self.minimums = minimums
        self.alpha = alpha

    def predict(
        self, *, actor: Mapping[str, Any], emission_state: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        phase = emission_state["white6603_phase"]
        age_bucket = _delay_bucket(emission_state["white6603_age_ms"])
        prior_start = int(emission_state["actor_has_prior_direct_hostile_start"])
        hero_class = actor["class"]
        spec = actor["spec_key"]
        contexts = (
            ("CLASS_SPEC", hero_class, spec, phase, age_bucket, prior_start),
            ("CLASS", hero_class, phase, age_bucket, prior_start),
            ("CLASS_PHASE", hero_class, phase),
            ("GLOBAL", phase),
        )
        for context in contexts:
            counts = self.counts.get(context)
            support = sum(counts.values()) if counts else 0
            if support >= self.minimums[context[0]]:
                white = counts[True]
                return {
                    "context": list(context),
                    "support": support,
                    "white_count": white,
                    "nonwhite_count": counts[False],
                    "alpha": self.alpha,
                    "probability": (white + self.alpha) / (support + 2 * self.alpha),
                }
        return None
