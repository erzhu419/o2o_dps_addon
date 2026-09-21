from collections import Counter

import pytest

from o2o_dps.development_white6603_runtime_head_v8 import (
    DevelopmentWhite6603CountHeadV8,
)


COUNTS = {("GLOBAL", "FIRST"): Counter({True: 9, False: 1})}
MINIMUMS = {"CLASS_SPEC": 1, "CLASS": 1, "CLASS_PHASE": 1, "GLOBAL": 1}
ACTOR = {"class": "WARRIOR", "spec_key": "WARRIOR_FURY"}
PREFIX = {
    "white6603_phase": "FIRST",
    "white6603_age_ms": 100,
    "actor_has_prior_direct_hostile_start": False,
}


def test_frozen_alpha_one_default_matches_explicit_one() -> None:
    default = DevelopmentWhite6603CountHeadV8(COUNTS, minimums=MINIMUMS).predict(
        actor=ACTOR, emission_state=PREFIX
    )
    explicit = DevelopmentWhite6603CountHeadV8(
        COUNTS, minimums=MINIMUMS, alpha=1.0
    ).predict(actor=ACTOR, emission_state=PREFIX)
    assert default == explicit
    assert default["alpha"] == 1.0
    assert default["probability"] == 10 / 12


def test_frozen_alpha_half_uses_its_selected_probability() -> None:
    selected = DevelopmentWhite6603CountHeadV8(
        COUNTS, minimums=MINIMUMS, alpha=0.5
    ).predict(actor=ACTOR, emission_state=PREFIX)
    assert selected["alpha"] == 0.5
    assert selected["probability"] == 9.5 / 11
    with pytest.raises(ValueError, match="frozen candidates"):
        DevelopmentWhite6603CountHeadV8(COUNTS, minimums=MINIMUMS, alpha=0.75)
