from __future__ import annotations

import json
import hashlib
import io
from pathlib import Path
import tarfile
import tempfile
import pytest

from scripts import development_teammate_current_retrain_v1 as retrain
from tests.test_chronicle_external_teammate_response_hpc_v1 import (
    TEST_SOURCE_CLOSURE_SHA256,
    _build_fixture,
)


def test_prepare_reuses_frozen_cohort_and_split_but_isolates_current_output() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        shared, _manifest, _overlay, _split, old, _path = _build_fixture(
            Path(temporary)
        )
        old["revision"] = "previous_frozen_revision"
        old_dispatch = shared / "frozen-old-dispatch.json"
        old_dispatch.write_text(json.dumps(old), encoding="utf-8")

        prepared = retrain.prepare(shared, old_dispatch, TEST_SOURCE_CLOSURE_SHA256)

        new_dispatch = json.loads(Path(prepared["dispatch"]).read_text())
        assert prepared["status"] == "PREPARED_NOT_LAUNCHED"
        assert prepared["task_count"] == len(old["tasks"])
        assert {row["instance_id"] for row in new_dispatch["tasks"]} == {
            row["instance_id"] for row in old["tasks"]
        }
        assert new_dispatch["split_contract"] == old["split_contract"]
        assert new_dispatch["revision"] == retrain.hpc.REVISION
        assert new_dispatch["output_root_locator"] != old["output_root_locator"]
        assert not Path(prepared["output_root"]).exists()
        progress = retrain.status_remote(shared, Path(prepared["dispatch"]))
        assert len(progress["tasks"]) == prepared["task_count"]
        assert {row["state"] for row in progress["tasks"]} == {"pending"}


def test_source_archive_is_deterministic_and_contains_exact_current_closure() -> None:
    first, digest = retrain._source_archive()
    second, second_digest = retrain._source_archive()
    assert first == second
    assert digest == second_digest == hashlib.sha256(first).hexdigest()
    with tarfile.open(fileobj=io.BytesIO(first)) as archive:
        assert sorted(archive.getnames()) == sorted(retrain.hpc.SOURCE_MODULE_PATHS)


def test_smoke_rejects_instance_outside_train_split() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        shared, _manifest, _overlay, _split, old, _path = _build_fixture(
            Path(temporary)
        )
        dispatch_path = shared / "dispatch.json"
        dispatch_path.write_text(json.dumps(old), encoding="utf-8")
        with pytest.raises(ValueError, match="not a TRAIN task"):
            retrain.smoke(shared, dispatch_path, "not-a-train-instance")
