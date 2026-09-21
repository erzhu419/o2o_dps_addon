from __future__ import annotations

from copy import deepcopy
import json

import pytest

import scripts.development_offline_wave_policy_d900_d5_remote_v1 as d5_remote

from scripts.development_offline_wave_policy_d900_d5_remote_v1 import (
    CANDIDATE_PANEL_REMOTE,
    CONFIRMATION_PLAN,
    EXPECTED_CONFIRMATION_LANES,
    EXPECTED_SELECTION_LANES,
    FROZEN_REMOTE,
    IMPLEMENTATION_REVISION,
    MAX_CANDIDATE_WORKERS,
    NODES,
    SELECTION_PLAN,
    SELECTION_PROCESSES_PER_NODE,
    SELECTION_RECEIPT_REMOTE,
    _expected_shard_identity_v1,
    _resume_preflight_v1,
    _run_node_batch_v1,
    _selection_panel_authority_v1,
    _validated_rows_v1,
    build_node_batch_command_v1,
    build_seed_job_argv_v1,
    build_seed_job_specs_v1,
    plan_receipt_v1,
)


PANEL_SHA256 = "a" * 64
SELECTION_SHA256 = "b" * 64
D4_IDENTITY = {
    "frozen_source_binding": {"component_id": "frozen"},
    "request_sha256": "request",
    "dynamic_config_sha256": "config",
    "evaluation_build_ref": "build",
    "target_rule_id": "target",
}


def _selection_digest(plan):
    return SELECTION_SHA256 if plan is CONFIRMATION_PLAN else None


def _expected_identities(plan, specs):
    return {
        spec.job_id: _expected_shard_identity_v1(
            plan,
            spec,
            candidate_panel_sha256=PANEL_SHA256,
            selection_receipt_sha256=_selection_digest(plan),
            d4_source=D4_IDENTITY,
        )
        for spec in specs
    }


def test_selection_is_one_seed_per_process_on_all_six_nodes():
    specs = build_seed_job_specs_v1(SELECTION_PLAN)
    assert len(NODES) == 6
    assert len(specs) == 12
    assert all(spec.lane_workers == MAX_CANDIDATE_WORKERS for spec in specs)
    assert {
        node: sum(spec.node == node for spec in specs) for node in NODES
    } == {node: SELECTION_PROCESSES_PER_NODE for node in NODES}
    assert all(len(spec.seed_pairs) == 1 for spec in specs)
    assert [pair for spec in specs for pair in spec.seed_pairs] == list(
        SELECTION_PLAN.seed_pairs
    )


def test_confirmation_is_48_one_seed_processes_and_seven_lanes():
    specs = build_seed_job_specs_v1(CONFIRMATION_PLAN)
    assert len(specs) == 48
    assert {node: sum(spec.node == node for spec in specs) for node in NODES} == {
        node: 8 for node in NODES
    }
    assert all(spec.lane_workers == EXPECTED_CONFIRMATION_LANES for spec in specs)
    assert CONFIRMATION_PLAN.lane_count == 7


def test_selection_and_confirmation_seed_components_are_fresh_and_disjoint():
    selection_sim = {row[0] for row in SELECTION_PLAN.seed_pairs}
    selection_team = {row[1] for row in SELECTION_PLAN.seed_pairs}
    confirmation_sim = {row[0] for row in CONFIRMATION_PLAN.seed_pairs}
    confirmation_team = {row[1] for row in CONFIRMATION_PLAN.seed_pairs}
    assert not selection_sim & confirmation_sim
    assert not selection_team & confirmation_team
    assert min(selection_sim) > 2_026_101_096
    assert min(confirmation_sim) > max(selection_sim)
    assert all(team - simulator == 100_000 for simulator, team in (
        *SELECTION_PLAN.seed_pairs,
        *CONFIRMATION_PLAN.seed_pairs,
    ))


def test_selection_argv_loads_frozen_panel_but_not_confirmation_receipt():
    spec = build_seed_job_specs_v1(SELECTION_PLAN)[0]
    argv = build_seed_job_argv_v1(
        SELECTION_PLAN,
        spec,
        candidate_panel_sha256=PANEL_SHA256,
        selection_receipt_sha256=None,
    )
    assert "--simulator-seed" not in argv
    assert "--teammate-seed" not in argv
    assert "--seed-count" not in argv
    assert argv[argv.index("--phase") + 1] == "selection"
    assert argv[argv.index("--candidate-panel") + 1] == CANDIDATE_PANEL_REMOTE
    assert argv[argv.index("--frozen-d3-result") + 1] == FROZEN_REMOTE
    assert argv[argv.index("--heldout-seed") + 1] == str(spec.first_seed)
    assert argv[argv.index("--heldout-seed-count") + 1] == "1"
    assert argv[argv.index("--lane-workers") + 1] == "64"
    assert argv[argv.index("--implementation-revision") + 1] == (
        IMPLEMENTATION_REVISION
    )
    assert argv[argv.index("--candidate-panel-sha256") + 1] == PANEL_SHA256
    assert "--selection-receipt" not in argv


def test_confirmation_argv_cannot_run_without_frozen_selection_receipt():
    spec = build_seed_job_specs_v1(CONFIRMATION_PLAN)[0]
    argv = build_seed_job_argv_v1(
        CONFIRMATION_PLAN,
        spec,
        candidate_panel_sha256=PANEL_SHA256,
        selection_receipt_sha256=SELECTION_SHA256,
    )
    assert argv[argv.index("--phase") + 1] == "confirmation"
    assert argv[argv.index("--selection-receipt") + 1] == (
        SELECTION_RECEIPT_REMOTE
    )
    assert argv[argv.index("--selection-receipt-sha256") + 1] == (
        SELECTION_SHA256
    )
    assert argv[argv.index("--lane-workers") + 1] == "7"


def test_selection_node_batch_has_two_process_cap_and_checkpoint_files():
    specs = tuple(
        spec
        for spec in build_seed_job_specs_v1(SELECTION_PLAN)
        if spec.node == NODES[0]
    )
    command = build_node_batch_command_v1(
        SELECTION_PLAN,
        specs,
        candidate_panel_sha256=PANEL_SHA256,
        selection_receipt_sha256=None,
    )
    assert len(specs) == 2
    assert command.count(" & pids=\"$pids $!\"") == 2
    assert command.count("if test ! -s ") == 2
    assert "for pid in $pids; do wait $pid || status=1; done" in command
    with pytest.raises(ValueError, match="two-process cap"):
        build_node_batch_command_v1(
            SELECTION_PLAN,
            (*specs, deepcopy(specs[0])),
            candidate_panel_sha256=PANEL_SHA256,
            selection_receipt_sha256=None,
        )


def test_confirmation_node_batch_launches_eight_seed_processes():
    specs = tuple(
        spec
        for spec in build_seed_job_specs_v1(CONFIRMATION_PLAN)
        if spec.node == NODES[0]
    )
    command = build_node_batch_command_v1(
        CONFIRMATION_PLAN,
        specs,
        candidate_panel_sha256=PANEL_SHA256,
        selection_receipt_sha256=SELECTION_SHA256,
    )
    assert len(specs) == 8
    assert command.count(" & pids=\"$pids $!\"") == 8


def test_plan_receipt_freezes_panel_workers_and_cohort_boundary():
    receipt = plan_receipt_v1(("selection", "confirmation"))
    assert receipt["candidate_panel_size"] == EXPECTED_SELECTION_LANES == 256
    assert receipt["searched_candidate_budget"] == 254
    assert receipt["selection_and_confirmation_seed_components_disjoint"] is True
    assert receipt["candidates_frozen_before_seed_execution"] is True
    assert receipt["confirmation_cannot_reselect"] is True
    assert receipt["phase_plans"]["selection"]["jobs_by_node"] == {
        node: 2 for node in NODES
    }
    assert receipt["phase_plans"]["confirmation"]["jobs_by_node"] == {
        node: 8 for node in NODES
    }


def _shard(plan, spec):
    result = dict(_expected_identities(plan, (spec,))[spec.job_id])
    result.update({
        "endpoint_rows": [
            {"row": index} for index in range(plan.lane_count)
        ],
        "runtime_summaries": [
            {
                "controller_id": f"lane-{index}",
                "candidate_id": f"candidate-{index}",
                "wall_seconds": 1.0,
                "accepted_action_count": 1,
            }
            for index in range(plan.lane_count)
        ],
        "contracts": {"compact_runtime_summaries": True},
    })
    return result


def test_merge_surface_requires_exact_one_seed_and_lane_count_per_shard():
    specs = build_seed_job_specs_v1(SELECTION_PLAN)
    results = {spec.job_id: _shard(SELECTION_PLAN, spec) for spec in specs}
    rows = _validated_rows_v1(
        SELECTION_PLAN,
        specs,
        results,
        expected_identities=_expected_identities(SELECTION_PLAN, specs),
    )
    assert len(rows) == 12 * 256

    bad = dict(results)
    target = specs[0]
    broken = deepcopy(bad[target.job_id])
    broken["endpoint_rows"].pop()
    bad[target.job_id] = broken
    with pytest.raises(ValueError, match="wrong lane count"):
        _validated_rows_v1(
            SELECTION_PLAN,
            specs,
            bad,
            expected_identities=_expected_identities(SELECTION_PLAN, specs),
        )


def test_merge_surface_rejects_stale_seed_checkpoint():
    specs = build_seed_job_specs_v1(CONFIRMATION_PLAN)
    results = {spec.job_id: _shard(CONFIRMATION_PLAN, spec) for spec in specs}
    bad = dict(results)
    target = specs[4]
    broken = deepcopy(bad[target.job_id])
    broken["executed_seed_pairs"][0]["simulator_seed"] += 1
    bad[target.job_id] = broken
    with pytest.raises(ValueError, match="stale shard identity"):
        _validated_rows_v1(
            CONFIRMATION_PLAN,
            specs,
            bad,
            expected_identities=_expected_identities(CONFIRMATION_PLAN, specs),
        )


def test_merge_surface_rejects_stale_candidate_panel_digest():
    specs = build_seed_job_specs_v1(SELECTION_PLAN)
    results = {spec.job_id: _shard(SELECTION_PLAN, spec) for spec in specs}
    target = specs[0]
    results[target.job_id]["candidate_panel_sha256"] = "stale"
    with pytest.raises(ValueError, match="stale shard identity"):
        _validated_rows_v1(
            SELECTION_PLAN,
            specs,
            results,
            expected_identities=_expected_identities(SELECTION_PLAN, specs),
        )


def test_merge_surface_rejects_duplicate_bulky_runtime_trace():
    specs = build_seed_job_specs_v1(SELECTION_PLAN)
    results = {spec.job_id: _shard(SELECTION_PLAN, spec) for spec in specs}
    target = specs[0]
    broken = deepcopy(results[target.job_id])
    broken["runtime_summaries"][0]["accepted_execution_trace"] = {
        "decisions": []
    }
    results[target.job_id] = broken
    with pytest.raises(ValueError, match="bulky duplicate traces"):
        _validated_rows_v1(
            SELECTION_PLAN,
            specs,
            results,
            expected_identities=_expected_identities(SELECTION_PLAN, specs),
        )


def test_confirmation_panel_uses_embedded_selection_authority(
    tmp_path, monkeypatch
):
    local = tmp_path / "panel.json"
    local.write_text(json.dumps({"panel": "frozen"}), encoding="utf-8")
    monkeypatch.setattr(d5_remote, "CANDIDATE_PANEL_LOCAL", local)
    selection = {"candidate_panel": {"panel": "frozen"}}
    assert _selection_panel_authority_v1(selection) == {"panel": "frozen"}

    local.write_text(json.dumps({"panel": "different"}), encoding="utf-8")
    with pytest.raises(ValueError, match="differs from the selection artifact"):
        _selection_panel_authority_v1(selection)


class _FakeScheduler:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def run_on(self, node, command, **kwargs):
        self.calls.append((node, command, kwargs))
        return self.result


def test_resume_preflight_accepts_only_matching_compact_identity():
    spec = build_seed_job_specs_v1(SELECTION_PLAN)[0]
    expected = _expected_identities(SELECTION_PLAN, (spec,))
    path = f"{SELECTION_PLAN.remote_result_dir}/{spec.job_id}.json"
    stdout = json.dumps(
        {"path": path, "identity": expected[spec.job_id]},
        separators=(",", ":"),
    )
    scheduler = _FakeScheduler((0, stdout + "\n", ""))
    _resume_preflight_v1(scheduler, SELECTION_PLAN, (spec,), expected)

    stale = deepcopy(expected[spec.job_id])
    stale["candidate_panel_sha256"] = "stale"
    scheduler = _FakeScheduler(
        (0, json.dumps({"path": path, "identity": stale}) + "\n", "")
    )
    with pytest.raises(RuntimeError, match="checkpoint identity mismatch"):
        _resume_preflight_v1(scheduler, SELECTION_PLAN, (spec,), expected)


def test_transport_failure_does_not_blindly_relaunch_batch():
    specs = tuple(
        spec
        for spec in build_seed_job_specs_v1(SELECTION_PLAN)
        if spec.node == NODES[0]
    )
    scheduler = _FakeScheduler((255, "", "connection lost"))
    with pytest.raises(RuntimeError, match="rc=255"):
        _run_node_batch_v1(
            scheduler,
            SELECTION_PLAN,
            NODES[0],
            specs,
            candidate_panel_sha256=PANEL_SHA256,
            selection_receipt_sha256=None,
        )
    assert len(scheduler.calls) == 1
