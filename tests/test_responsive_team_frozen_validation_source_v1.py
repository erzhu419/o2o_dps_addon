from copy import deepcopy

import pytest

from o2o_dps.responsive_team_frozen_validation_source_v1 import (
    verify_frozen_validation_source_v1,
)


INSTANCE = "d900"
COMPONENT = "heldout-component"
LOCATOR = "stage5/d900.json.gz"
STAGE5_SHA = "stage5-sha"
PARTITION_SHA = "partition-sha"


def _dispatch():
    return {
        "tasks": [
            {
                "instance_id": INSTANCE,
                "component_id": COMPONENT,
                "split": "VALIDATION",
                "partition_locator": LOCATOR,
                "partition": {"compressed_file_sha256": PARTITION_SHA},
                "instance_entry_content_sha256": "entry-sha",
            }
        ],
        "split_contract": {"validation_component_ids": [COMPONENT]},
        "source_bindings": {"stage5": {"content_sha256": STAGE5_SHA}},
    }


def _verify(dispatch, **overrides):
    selected = {
        "instance_id": INSTANCE,
        "component_id": COMPONENT,
        "partition_locator": LOCATOR,
        "stage5_content_sha256": STAGE5_SHA,
        "partition_compressed_file_sha256": PARTITION_SHA,
    }
    selected.update(overrides)
    return verify_frozen_validation_source_v1(dispatch, **selected)


def test_frozen_validation_source_accepts_exact_binding():
    binding = _verify(_dispatch())
    assert binding["instance_id"] == INSTANCE
    assert binding["instance_entry_content_sha256"] == "entry-sha"


@pytest.mark.parametrize(
    "mutation, selected",
    [
        (lambda d: d["tasks"].append(deepcopy(d["tasks"][0])), {}),
        (lambda d: d["tasks"][0].update(split="TRAIN"), {}),
        (lambda d: d["split_contract"].update(validation_component_ids=[]), {}),
        (lambda d: None, {"component_id": "other"}),
        (lambda d: None, {"partition_locator": "stage5/other.json.gz"}),
        (lambda d: None, {"stage5_content_sha256": "other"}),
        (lambda d: None, {"partition_compressed_file_sha256": "other"}),
    ],
)
def test_frozen_validation_source_rejects_changed_binding(mutation, selected):
    dispatch = _dispatch()
    mutation(dispatch)
    with pytest.raises(ValueError):
        _verify(dispatch, **selected)
