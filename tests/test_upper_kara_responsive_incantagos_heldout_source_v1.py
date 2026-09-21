from __future__ import annotations

import pytest

from o2o_dps.upper_kara_responsive_incantagos_case_v1 import (
    UpperKaraResponsiveIncantagosCaseV1Error,
    compile_responsive_incantagos_case_v1,
)
from test_upper_kara_responsive_incantagos_case_v1 import _fixed_case, _membership, _wave


def _validation_source_membership() -> dict:
    evidence = _membership()
    evidence["component_id"] = "component-frozen-validation"
    evidence["split"] = "VALIDATION"
    evidence["model_training_held_out"] = True
    return evidence


def test_validation_model_source_does_not_claim_heldout_performance() -> None:
    evidence = _validation_source_membership()
    compiled = compile_responsive_incantagos_case_v1(
        _fixed_case(), _wave(), source_membership_evidence=evidence
    )

    source = compiled.receipt["source"]["source_membership_evidence"]
    assert source["split"] == "VALIDATION"
    assert source["model_training_held_out"] is True
    assert source["heldout_performance_evidence_eligible"] is False
    assert source["comparison_authorized"] is False
    assert compiled.receipt["scientific_boundaries"]["heldout_performance_evidence_eligible"] is False
    assert compiled.receipt["scientific_boundaries"]["comparison_authorized"] is False


def test_train_source_cannot_be_mislabeled_model_training_held_out() -> None:
    evidence = _validation_source_membership()
    evidence["split"] = "TRAIN"
    with pytest.raises(UpperKaraResponsiveIncantagosCaseV1Error):
        compile_responsive_incantagos_case_v1(
            _fixed_case(), _wave(), source_membership_evidence=evidence
        )
