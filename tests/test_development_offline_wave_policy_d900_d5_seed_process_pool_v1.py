from __future__ import annotations

import argparse
from concurrent.futures import Future
import multiprocessing
from types import SimpleNamespace

import pytest

import scripts.development_offline_wave_policy_d900_d5_seed_v1 as d5_seed
from o2o_dps.offline_wave_d4_all_seed_endpoint_v1 import _invalid_terminal
from o2o_dps.offline_wave_d5_selection_v1 import (
    OfflineWaveD5SelectionV1Error,
)


def _fake_replay_for_real_fork(*, controller, simulator_seed, teammate_seed, **_):
    return {
        "runtime_summary": {
            "controller_id": controller.controller_id,
            "candidate_id": controller.candidate_id,
            "simulator_seed": simulator_seed,
            "teammate_seed": teammate_seed,
        }
    }


def _controllers(count: int):
    return tuple(
        SimpleNamespace(
            controller_id=f"controller-{index:03d}",
            candidate_id=f"candidate-{index:03d}",
        )
        for index in range(count)
    )


@pytest.mark.parametrize(
    ("controller_count", "workers"),
    ((256, 48), (7, 7)),
    ids=("selection", "confirmation"),
)
def test_fork_pool_inherits_parent_context_and_submits_exact_indices(
    monkeypatch, controller_count, workers
):
    controllers = _controllers(controller_count)
    args = argparse.Namespace(marker="args")
    environment = SimpleNamespace(marker="environment")
    fork_context = object()
    observed = {"indices": [], "replays": []}

    def fake_get_context(method):
        assert method == "fork"
        return fork_context

    def fake_replay(*, args, environment, controller, simulator_seed, teammate_seed):
        observed["replays"].append(
            (args, environment, controller, simulator_seed, teammate_seed)
        )
        return {
            "runtime_summary": {
                "controller_id": controller.controller_id,
                "candidate_id": controller.candidate_id,
            }
        }

    class FakeProcessPoolExecutor:
        def __init__(self, *, max_workers, mp_context):
            assert max_workers == workers
            assert mp_context is fork_context
            inherited = d5_seed._FORK_REPLAY_CONTEXT_V1
            assert inherited is not None
            assert inherited.args is args
            assert inherited.environment is environment
            assert inherited.controllers is controllers

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def submit(self, function, index):
            assert function is d5_seed._replay_controller_index_v1
            observed["indices"].append(index)
            future = Future()
            future.set_result(function(index))
            return future

    monkeypatch.setattr(d5_seed.multiprocessing, "get_context", fake_get_context)
    monkeypatch.setattr(d5_seed, "ProcessPoolExecutor", FakeProcessPoolExecutor)
    monkeypatch.setattr(d5_seed, "_replay_controller_v1", fake_replay)

    jobs = d5_seed._run_replays_in_fork_pool_v1(
        args=args,
        environment=environment,
        controllers=controllers,
        simulator_seed=2_026_102_049,
        teammate_seed=2_026_202_049,
        workers=workers,
    )

    assert observed["indices"] == list(range(controller_count))
    assert [row[2] for row in observed["replays"]] == list(controllers)
    assert all(row[0] is args for row in observed["replays"])
    assert all(row[1] is environment for row in observed["replays"])
    assert all(row[3:] == (2_026_102_049, 2_026_202_049) for row in observed["replays"])
    assert {
        (
            row["runtime_summary"]["controller_id"],
            row["runtime_summary"]["candidate_id"],
        )
        for row in jobs
    } == {
        (controller.controller_id, controller.candidate_id)
        for controller in controllers
    }
    assert d5_seed._FORK_REPLAY_CONTEXT_V1 is None


def test_fork_pool_propagates_worker_exception_and_clears_parent_context(monkeypatch):
    controllers = _controllers(2)

    class FailingProcessPoolExecutor:
        def __init__(self, *, max_workers, mp_context):
            assert max_workers == 2

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def submit(self, function, index):
            future = Future()
            if index == 1:
                future.set_exception(RuntimeError("lane failed"))
            else:
                future.set_result({"index": index})
            return future

    monkeypatch.setattr(d5_seed.multiprocessing, "get_context", lambda _: object())
    monkeypatch.setattr(
        d5_seed, "ProcessPoolExecutor", FailingProcessPoolExecutor
    )

    with pytest.raises(RuntimeError, match="lane failed"):
        d5_seed._run_replays_in_fork_pool_v1(
            args=argparse.Namespace(),
            environment=SimpleNamespace(),
            controllers=controllers,
            simulator_seed=1,
            teammate_seed=100_001,
            workers=2,
        )
    assert d5_seed._FORK_REPLAY_CONTEXT_V1 is None


def test_invalid_selection_endpoint_reports_candidate_and_terminal_reason():
    terminal = _invalid_terminal(
        "CausalActionProgramError: program exceeded max_decisions=300",
        horizon_ms=15_531,
    )
    with pytest.raises(OfflineWaveD5SelectionV1Error) as caught:
        d5_seed._build_endpoint_row_v1(
            phase="selection",
            candidate_id="d5-searched-042",
            controller_id="PI_STAR",
            simulator_seed=2_026_102_003,
            teammate_seed=2_026_202_003,
            replay_status="INVALID",
            terminal_endpoint=terminal,
            domain_fallback_calls=0,
            requires_zero_fallback=True,
            action_attribution={},
        )
    message = str(caught.value)
    assert "d5-searched-042" in message
    assert "program exceeded max_decisions=300" in message
    assert '"replay_status": "INVALID"' in message


def test_invalid_confirmation_endpoint_reports_controller_and_seed():
    terminal = _invalid_terminal("bridge exited unexpectedly", horizon_ms=15_531)
    with pytest.raises(OfflineWaveD5SelectionV1Error) as caught:
        d5_seed._build_endpoint_row_v1(
            phase="confirmation",
            candidate_id="frozen-winner",
            controller_id="CAT",
            simulator_seed=2_026_102_049,
            teammate_seed=2_026_202_049,
            replay_status="INVALID",
            terminal_endpoint=terminal,
            domain_fallback_calls=None,
            requires_zero_fallback=False,
            action_attribution=None,
        )
    message = str(caught.value)
    assert '"controller_id": "CAT"' in message
    assert '"simulator_seed": 2026102049' in message
    assert "bridge exited unexpectedly" in message


@pytest.mark.skipif(
    "fork" not in multiprocessing.get_all_start_methods(),
    reason="real fork smoke requires a Linux fork context",
)
def test_real_fork_smoke_uses_inherited_objects_without_starting_bridge(monkeypatch):
    controllers = _controllers(2)
    monkeypatch.setattr(
        d5_seed, "_replay_controller_v1", _fake_replay_for_real_fork
    )

    jobs = d5_seed._run_replays_in_fork_pool_v1(
        args=argparse.Namespace(),
        environment=SimpleNamespace(),
        controllers=controllers,
        simulator_seed=17,
        teammate_seed=100_017,
        workers=2,
    )

    assert {
        (
            row["runtime_summary"]["controller_id"],
            row["runtime_summary"]["candidate_id"],
            row["runtime_summary"]["simulator_seed"],
            row["runtime_summary"]["teammate_seed"],
        )
        for row in jobs
    } == {
        (controller.controller_id, controller.candidate_id, 17, 100_017)
        for controller in controllers
    }
