"""Run the frozen d900 D5 selection and fresh confirmation campaigns.

Candidate programs are materialized once on the coordinating host.  Every
remote process evaluates exactly one paired seed.  Selection shards evaluate
the same 256-program manifest with at most 64 concurrent lanes; confirmation
shards evaluate only the frozen winner and its six registered comparators.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import json
from pathlib import Path
import shlex
import subprocess
import sys
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from o2o_dps.offline_wave_searched_program_v1 import (
    searched_wave_program_from_dict_v1,
)
from o2o_dps.fury_paired_multiseed_runner_v2 import sha256_json
from scripts.development_offline_wave_policy_d900_d2_remote_v1 import (
    RUNTIME,
    _d2_argv,
)
from scripts.development_offline_wave_policy_d900_d3_frozen_heldout_remote_v1 import (
    FROZEN_LOCAL,
    FROZEN_REMOTE,
)


JSONMap = dict[str, Any]
SCHEMA = "development_offline_wave_policy_d900_d5_multinode/v1"
IMPLEMENTATION_REVISION = "d900-d5-one-seed-shard-v2"
SEED_OUTPUT_SCHEMA = "development_offline_wave_policy_d900_d5_seed/v1"
PLAN_SCHEMA = f"{SCHEMA}/plan"
CANDIDATE_PANEL_SCHEMA = f"{SCHEMA}/candidate_panel"
NODES = ("node001", "node002", "node003", "node004", "node005", "node006")
FIXED_HORIZON_MS = 15_531
SEARCHED_CANDIDATE_BUDGET = 254
EXPECTED_SELECTION_LANES = 256
EXPECTED_CONFIRMATION_LANES = 7
MAX_CANDIDATE_WORKERS = 64
SELECTION_PROCESSES_PER_NODE = 2
SEED_PARTNER_OFFSET = 100_000
MAX_DECISIONS = 300

D4_LOCAL = (
    ROOT
    / "results/offline-wave-policy-v1/"
    "d900-d4-fresh-all-seed-2026101049-2026101096-v1.json"
)
D4_REMOTE = (
    f"{RUNTIME}/results/offline-wave-policy-v1/"
    "d900-d4-fresh-all-seed-2026101049-2026101096-v1.json"
)
CANDIDATE_PANEL_LOCAL = (
    ROOT
    / "results/offline-wave-policy-v1/"
    "d900-d5-frozen-candidate-panel-v1.json"
)
CANDIDATE_PANEL_REMOTE = (
    f"{RUNTIME}/results/offline-wave-policy-v1/"
    "d900-d5-frozen-candidate-panel-v1.json"
)
SELECTION_RECEIPT_LOCAL = (
    ROOT
    / "results/offline-wave-policy-v1/"
    "d900-d5-selection-2026102001-2026102012-v1.json"
)
SELECTION_RECEIPT_REMOTE = (
    f"{RUNTIME}/results/offline-wave-policy-v1/"
    "d900-d5-selection-2026102001-2026102012-v1.json"
)
CONFIRMATION_OUTPUT_LOCAL = (
    ROOT
    / "results/offline-wave-policy-v1/"
    "d900-d5-confirmation-2026102049-2026102096-v1.json"
)

SOURCE_FILES = (
    "o2o_dps/offline_wave_d4_all_seed_endpoint_v1.py",
    "o2o_dps/offline_wave_d5_candidate_panel_v1.py",
    "o2o_dps/offline_wave_d5_selection_v1.py",
    "scripts/development_offline_wave_policy_d900_d5_seed_v1.py",
)


@dataclass(frozen=True)
class CampaignPlanV1:
    phase: str
    campaign_id: str
    expansion_id: str
    first_seed: int
    seed_count: int
    lane_count: int
    lane_workers: int

    @property
    def seed_pairs(self) -> tuple[tuple[int, int], ...]:
        return tuple(
            (seed, seed + SEED_PARTNER_OFFSET)
            for seed in range(self.first_seed, self.first_seed + self.seed_count)
        )

    @property
    def remote_result_dir(self) -> str:
        return f"{RUNTIME}/results/offline-wave-policy-v1/{self.expansion_id}"


SELECTION_PLAN = CampaignPlanV1(
    phase="selection",
    campaign_id="d900-d5-all-seed-selection-v1",
    expansion_id="d900-d5-selection-shards-2026102001-2026102012-v1",
    first_seed=2_026_102_001,
    seed_count=12,
    lane_count=EXPECTED_SELECTION_LANES,
    lane_workers=MAX_CANDIDATE_WORKERS,
)
CONFIRMATION_PLAN = CampaignPlanV1(
    phase="confirmation",
    campaign_id="d900-d5-fresh-confirmation-v1",
    expansion_id="d900-d5-confirmation-shards-2026102049-2026102096-v1",
    first_seed=2_026_102_049,
    seed_count=48,
    lane_count=EXPECTED_CONFIRMATION_LANES,
    lane_workers=EXPECTED_CONFIRMATION_LANES,
)
PLANS = {
    SELECTION_PLAN.phase: SELECTION_PLAN,
    CONFIRMATION_PLAN.phase: CONFIRMATION_PLAN,
}


@dataclass(frozen=True)
class SeedJobSpecV1:
    job_id: str
    node: str
    first_seed: int
    lane_workers: int

    @property
    def seed_pairs(self) -> tuple[tuple[int, int], ...]:
        return ((self.first_seed, self.first_seed + SEED_PARTNER_OFFSET),)


def build_seed_job_specs_v1(plan: CampaignPlanV1) -> tuple[SeedJobSpecV1, ...]:
    specs = tuple(
        SeedJobSpecV1(
            job_id=f"seed-{seed}",
            node=NODES[offset % len(NODES)],
            first_seed=seed,
            lane_workers=plan.lane_workers,
        )
        for offset, seed in enumerate(
            range(plan.first_seed, plan.first_seed + plan.seed_count)
        )
    )
    if tuple(pair for spec in specs for pair in spec.seed_pairs) != plan.seed_pairs:
        raise RuntimeError("one-seed shard plan differs from campaign seed order")
    counts = {
        node: sum(spec.node == node for spec in specs) for node in NODES
    }
    if plan is SELECTION_PLAN and set(counts.values()) != {
        SELECTION_PROCESSES_PER_NODE
    }:
        raise RuntimeError("selection must assign two seed processes per node")
    return specs


def _drop_option(argv: list[str], option: str) -> None:
    if option not in argv:
        return
    index = argv.index(option)
    if index + 1 >= len(argv):
        raise ValueError(f"{option} lacks a value")
    del argv[index : index + 2]


def build_seed_job_argv_v1(
    plan: CampaignPlanV1,
    spec: SeedJobSpecV1,
    *,
    candidate_panel_sha256: str,
    selection_receipt_sha256: str | None,
) -> list[str]:
    argv = _d2_argv()
    argv[1] = (
        f"{RUNTIME}/scripts/"
        "development_offline_wave_policy_d900_d5_seed_v1.py"
    )
    for option in (
        "--simulator-seed",
        "--teammate-seed",
        "--seed-count",
        "--lane-workers",
    ):
        _drop_option(argv, option)
    argv.extend(
        (
            "--phase",
            plan.phase,
            "--implementation-revision",
            IMPLEMENTATION_REVISION,
            "--candidate-panel-sha256",
            candidate_panel_sha256,
            "--candidate-panel",
            CANDIDATE_PANEL_REMOTE,
            "--frozen-d3-result",
            FROZEN_REMOTE,
            "--source-d4-result",
            D4_REMOTE,
            "--campaign-id",
            plan.campaign_id,
            "--campaign-first-seed",
            str(plan.first_seed),
            "--campaign-seed-count",
            str(plan.seed_count),
            "--heldout-seed",
            str(spec.first_seed),
            "--heldout-seed-count",
            "1",
            "--fixed-horizon-ms",
            str(FIXED_HORIZON_MS),
            "--max-decisions",
            str(MAX_DECISIONS),
            "--lane-workers",
            str(spec.lane_workers),
        )
    )
    if plan.phase == "confirmation":
        if selection_receipt_sha256 is None:
            raise ValueError("confirmation requires a selection receipt digest")
        argv.extend(("--selection-receipt", SELECTION_RECEIPT_REMOTE))
        argv.extend(("--selection-receipt-sha256", selection_receipt_sha256))
    elif selection_receipt_sha256 is not None:
        raise ValueError("selection must not bind a confirmation receipt digest")
    return argv


def _seed_result_path(plan: CampaignPlanV1, spec: SeedJobSpecV1) -> str:
    return f"{plan.remote_result_dir}/{spec.job_id}.json"


_ENVIRONMENT_IDENTITY_FIELDS = (
    "frozen_source_binding",
    "request_sha256",
    "dynamic_config_sha256",
    "evaluation_build_ref",
    "target_rule_id",
)


def _expected_shard_identity_v1(
    plan: CampaignPlanV1,
    spec: SeedJobSpecV1,
    *,
    candidate_panel_sha256: str,
    selection_receipt_sha256: str | None,
    d4_source: Mapping[str, Any],
) -> JSONMap:
    identity = {
        "schema": SEED_OUTPUT_SCHEMA,
        "status": "D5_ONE_SEED_SHARD_COMPLETE",
        "implementation_revision": IMPLEMENTATION_REVISION,
        "phase": plan.phase,
        "campaign_id": plan.campaign_id,
        "executed_seed_pairs": [
            {"simulator_seed": simulator, "teammate_seed": teammate}
            for simulator, teammate in spec.seed_pairs
        ],
        "candidate_panel_sha256": candidate_panel_sha256,
        "selection_receipt_sha256": selection_receipt_sha256,
        "candidate_panel_size": EXPECTED_SELECTION_LANES,
        "fixed_horizon_ms": FIXED_HORIZON_MS,
    }
    for field in _ENVIRONMENT_IDENTITY_FIELDS:
        value = d4_source.get(field)
        if value is None:
            raise ValueError(f"D4 source lacks {field}")
        identity[field] = value
    return identity


def build_node_batch_command_v1(
    plan: CampaignPlanV1,
    specs: Sequence[SeedJobSpecV1],
    *,
    candidate_panel_sha256: str,
    selection_receipt_sha256: str | None,
) -> str:
    if not specs or len({spec.node for spec in specs}) != 1:
        raise ValueError("one node batch requires at least one job on one node")
    if plan.phase == "selection" and len(specs) > SELECTION_PROCESSES_PER_NODE:
        raise ValueError("selection node batch exceeds its two-process cap")
    launches: list[str] = ["pids=''", "status=0"]
    logs: list[str] = []
    for spec in specs:
        output = _seed_result_path(plan, spec)
        temporary = output + ".tmp"
        log = f"{plan.remote_result_dir}/{spec.job_id}.log"
        command = (
            f"PYTHONDONTWRITEBYTECODE=1 PYTHONPATH={shlex.quote(RUNTIME)} "
            + shlex.join(
                build_seed_job_argv_v1(
                    plan,
                    spec,
                    candidate_panel_sha256=candidate_panel_sha256,
                    selection_receipt_sha256=selection_receipt_sha256,
                )
            )
        )
        launches.append(
            f"if test ! -s {shlex.quote(output)}; then "
            f"({command} > {shlex.quote(temporary)} 2> {shlex.quote(log)}"
            f" && mv {shlex.quote(temporary)} {shlex.quote(output)}) "
            f"& pids=\"$pids $!\"; fi"
        )
        logs.append(log)
    launches.extend(
        (
            "for pid in $pids; do wait $pid || status=1; done",
            "if test $status -ne 0; then "
            + " ".join(
                f"test ! -s {shlex.quote(log)} || tail -n 30 {shlex.quote(log)};"
                for log in logs
            )
            + " fi",
            "exit $status",
        )
    )
    return "; ".join(launches)


def plan_receipt_v1(phases: Sequence[str]) -> JSONMap:
    phase_rows: JSONMap = {}
    for phase in phases:
        plan = PLANS[phase]
        specs = build_seed_job_specs_v1(plan)
        phase_rows[phase] = {
            "campaign_id": plan.campaign_id,
            "expansion_id": plan.expansion_id,
            "first_seed": plan.first_seed,
            "seed_count": plan.seed_count,
            "lane_count_per_seed": plan.lane_count,
            "lane_workers_per_process": plan.lane_workers,
            "one_seed_per_process": True,
            "jobs_by_node": {
                node: sum(spec.node == node for spec in specs) for node in NODES
            },
            "maximum_candidate_lanes_per_node": max(
                sum(
                    spec.lane_workers
                    for spec in specs
                    if spec.node == node
                )
                for node in NODES
            ),
            "seed_pairs": [
                {"simulator_seed": simulator, "teammate_seed": teammate}
                for simulator, teammate in plan.seed_pairs
            ],
        }
    return {
        "schema": PLAN_SCHEMA,
        "phases": list(phases),
        "candidate_panel_size": EXPECTED_SELECTION_LANES,
        "searched_candidate_budget": SEARCHED_CANDIDATE_BUDGET,
        "fixed_horizon_ms": FIXED_HORIZON_MS,
        "phase_plans": phase_rows,
        "selection_and_confirmation_seed_components_disjoint": (
            not (
                {row[0] for row in SELECTION_PLAN.seed_pairs}
                & {row[0] for row in CONFIRMATION_PLAN.seed_pairs}
            )
            and not (
                {row[1] for row in SELECTION_PLAN.seed_pairs}
                & {row[1] for row in CONFIRMATION_PLAN.seed_pairs}
            )
        ),
        "candidates_frozen_before_seed_execution": True,
        "confirmation_cannot_reselect": True,
    }


def _offline_guide_actions(source: Mapping[str, Any]) -> list[JSONMap]:
    selected = source.get("selected_offline_donor_seed_pair")
    runs = source.get("proposal_offline_donor_runs")
    if not isinstance(selected, Mapping) or not isinstance(runs, list):
        raise ValueError("D3 source lacks its accepted offline donor run")
    matches = [
        row
        for row in runs
        if isinstance(row, Mapping)
        and row.get("simulator_seed") == selected.get("simulator_seed")
        and row.get("teammate_seed") == selected.get("teammate_seed")
    ]
    if len(matches) != 1:
        raise ValueError("D3 source does not identify exactly one donor run")
    trace = matches[0].get("accepted_execution_trace")
    guides = trace.get("guide_actions") if isinstance(trace, Mapping) else None
    if not isinstance(guides, list) or not guides:
        raise ValueError("selected D3 donor has no accepted guide actions")
    return [dict(row) for row in guides]


def build_candidate_panel_artifact_v1() -> JSONMap:
    from o2o_dps.offline_wave_d5_candidate_panel_v1 import (
        PARENT_CANDIDATE_ID,
        TAIL_ONLY_CANDIDATE_ID,
        build_d5_candidate_panel_v1,
        validate_d5_candidate_panel_v1,
    )
    from o2o_dps.offline_wave_d5_selection_v1 import (
        build_d5_selection_contract_v1,
    )

    d4 = json.loads(D4_LOCAL.read_text(encoding="utf-8"))
    d3 = json.loads(FROZEN_LOCAL.read_text(encoding="utf-8"))
    frozen = d4.get("contract", {}).get("frozen_candidate")
    if not isinstance(frozen, Mapping) or not isinstance(
        frozen.get("program"), Mapping
    ):
        raise ValueError("D4 source lacks its frozen candidate program")
    parent = searched_wave_program_from_dict_v1(frozen["program"])
    candidate_panel = validate_d5_candidate_panel_v1(
        build_d5_candidate_panel_v1(
            parent=parent,
            offline_guide_actions=_offline_guide_actions(d3),
            searched_candidate_budget=SEARCHED_CANDIDATE_BUDGET,
            horizon_ms=FIXED_HORIZON_MS,
        )
    )
    candidates = candidate_panel["manifests"]
    if len(candidates) != EXPECTED_SELECTION_LANES:
        raise RuntimeError(
            "D5 panel must contain 254 searched candidates plus retention and tail"
        )
    d4_pairs = d4.get("contract", {}).get("seed_pairs")
    selection_contract = build_d5_selection_contract_v1(
        campaign_id=SELECTION_PLAN.campaign_id,
        candidates=candidates,
        retention_candidate_id=PARENT_CANDIDATE_ID,
        tail_only_candidate_id=TAIL_ONLY_CANDIDATE_ID,
        selection_seed_pairs=SELECTION_PLAN.seed_pairs,
        heldout_seed_pairs=CONFIRMATION_PLAN.seed_pairs,
        d4_confirmation_seed_pairs=d4_pairs,
        horizon_ms=FIXED_HORIZON_MS,
    )
    return {
        "schema": CANDIDATE_PANEL_SCHEMA,
        "source_d4_candidate": dict(frozen),
        "candidate_panel": candidate_panel,
        "selection_contract": selection_contract,
        "fixed_horizon_ms": FIXED_HORIZON_MS,
        "searched_candidate_budget": SEARCHED_CANDIDATE_BUDGET,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "selection_seed_pairs": [
            {"simulator_seed": a, "teammate_seed": b}
            for a, b in SELECTION_PLAN.seed_pairs
        ],
        "confirmation_seed_pairs": [
            {"simulator_seed": a, "teammate_seed": b}
            for a, b in CONFIRMATION_PLAN.seed_pairs
        ],
        "candidates_frozen_before_seed_execution": True,
        "per_seed_candidate_generation": False,
    }


def _write_candidate_panel_v1() -> JSONMap:
    panel = build_candidate_panel_artifact_v1()
    CANDIDATE_PANEL_LOCAL.parent.mkdir(parents=True, exist_ok=True)
    CANDIDATE_PANEL_LOCAL.write_text(
        json.dumps(panel, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return panel


def _selection_panel_authority_v1(selection: Mapping[str, Any]) -> JSONMap:
    """Require the staged panel to equal the panel frozen into selection."""

    embedded = selection.get("candidate_panel")
    if not isinstance(embedded, Mapping):
        raise ValueError("selection artifact lacks its embedded candidate panel")
    if not CANDIDATE_PANEL_LOCAL.is_file():
        raise FileNotFoundError(CANDIDATE_PANEL_LOCAL)
    local = json.loads(CANDIDATE_PANEL_LOCAL.read_text(encoding="utf-8"))
    if not isinstance(local, Mapping) or local != embedded:
        raise ValueError(
            "local candidate panel differs from the selection artifact authority"
        )
    return dict(embedded)


def _candidate_panel_sha256_v1(panel: Mapping[str, Any]) -> str:
    if not isinstance(panel, Mapping):
        raise TypeError("candidate panel must be a mapping")
    return sha256_json(panel)


def _selection_receipt_sha256_v1(selection: Mapping[str, Any]) -> str:
    receipt = selection.get("selection_receipt")
    if not isinstance(receipt, Mapping):
        raise ValueError("selection artifact lacks selection_receipt")
    return sha256_json(receipt)


def _prepare_remote_dir_v1(
    scheduler: Any, plan: CampaignPlanV1, *, resume: bool
) -> None:
    command = (
        shlex.join(["mkdir", "-p", plan.remote_result_dir])
        if resume
        else f"test ! -e {shlex.quote(plan.remote_result_dir)} && "
        + shlex.join(["mkdir", "-p", plan.remote_result_dir])
    )
    rc, _stdout, stderr = scheduler.run_on(
        NODES[0], command, timeout=30, check=False
    )
    if rc:
        raise RuntimeError(
            f"cannot prepare {plan.expansion_id}: {stderr[-2000:]}"
        )


def _resume_preflight_v1(
    scheduler: Any,
    plan: CampaignPlanV1,
    specs: Sequence[SeedJobSpecV1],
    expected_identities: Mapping[str, Mapping[str, Any]],
) -> None:
    """Validate compact identities before a nonempty shard may be skipped."""

    fields = tuple(next(iter(expected_identities.values())).keys())
    program = (
        "import json,os,sys\n"
        f"fields={fields!r}\n"
        "for path in sys.argv[1:]:\n"
        " if os.path.isfile(path) and os.path.getsize(path)>0:\n"
        "  with open(path,encoding='utf-8') as stream: value=json.load(stream)\n"
        "  identity={field:value.get(field) for field in fields}\n"
        "  print(json.dumps({'path':path,'identity':identity},sort_keys=True,separators=(',',':')))\n"
    )
    paths = [_seed_result_path(plan, spec) for spec in specs]
    command = shlex.join([_d2_argv()[0], "-c", program, *paths])
    rc, stdout, stderr = scheduler.run_on(
        NODES[0], command, timeout=180, check=False
    )
    if rc:
        raise RuntimeError(
            f"cannot validate {plan.phase} resume identities: {stderr[-3000:]}"
        )
    expected_by_path = {
        _seed_result_path(plan, spec): expected_identities[spec.job_id]
        for spec in specs
    }
    seen: set[str] = set()
    for line in stdout.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        path = row.get("path") if isinstance(row, Mapping) else None
        identity = row.get("identity") if isinstance(row, Mapping) else None
        if path not in expected_by_path or path in seen:
            raise RuntimeError("resume preflight returned an unknown or repeated shard")
        seen.add(path)
        if identity != expected_by_path[path]:
            raise RuntimeError(
                f"resume checkpoint identity mismatch: {Path(path).name}"
            )


def _rsync_file_v1(scheduler: Any, source: Path, destination: str) -> None:
    if not source.is_file():
        raise FileNotFoundError(source)
    subprocess.run(
        [
            "rsync",
            "--archive",
            "--no-owner",
            "--no-group",
            "--no-perms",
            "-e",
            scheduler._ssh_rsync_shell_for_node(NODES[0]),
            str(source),
            f"{scheduler._ssh_target_for_node(NODES[0])}:{destination}",
        ],
        check=True,
        timeout=120,
    )


def _stage_small_inputs_v1(
    scheduler: Any, *, include_selection_receipt: bool
) -> None:
    for relative in SOURCE_FILES:
        _rsync_file_v1(scheduler, ROOT / relative, f"{RUNTIME}/{relative}")
    for source, destination in (
        (FROZEN_LOCAL, FROZEN_REMOTE),
        (D4_LOCAL, D4_REMOTE),
        (CANDIDATE_PANEL_LOCAL, CANDIDATE_PANEL_REMOTE),
    ):
        _rsync_file_v1(scheduler, source, destination)
    if include_selection_receipt:
        _rsync_file_v1(
            scheduler, SELECTION_RECEIPT_LOCAL, SELECTION_RECEIPT_REMOTE
        )


def _run_node_batch_v1(
    scheduler: Any,
    plan: CampaignPlanV1,
    node: str,
    specs: Sequence[SeedJobSpecV1],
    *,
    candidate_panel_sha256: str,
    selection_receipt_sha256: str | None,
) -> None:
    command = build_node_batch_command_v1(
        plan,
        specs,
        candidate_panel_sha256=candidate_panel_sha256,
        selection_receipt_sha256=selection_receipt_sha256,
    )
    rc, stdout, stderr = scheduler.run_on(
        node, command, timeout=3600, check=False
    )
    if rc:
        details = stderr[-8000:] if stderr.strip() else stdout[-8000:]
        raise RuntimeError(
            f"{plan.phase} batch on {node} failed (rc={rc}): {details}"
        )


def _fetch_seed_results_v1(
    scheduler: Any,
    plan: CampaignPlanV1,
    specs: Sequence[SeedJobSpecV1],
) -> dict[str, Mapping[str, Any]]:
    command = shlex.join(
        ["cat", *(_seed_result_path(plan, spec) for spec in specs)]
    )
    rc, stdout, stderr = scheduler.run_on(
        NODES[0], command, timeout=180, check=False
    )
    if rc:
        raise RuntimeError(f"cannot fetch {plan.phase} receipts: {stderr[-3000:]}")
    lines = [line for line in stdout.splitlines() if line.strip()]
    if len(lines) != len(specs):
        raise RuntimeError(
            f"expected {len(specs)} {plan.phase} receipts, got {len(lines)}"
        )
    result: dict[str, Mapping[str, Any]] = {}
    for spec, line in zip(specs, lines):
        value = json.loads(line)
        if not isinstance(value, dict):
            raise RuntimeError(f"{spec.job_id} receipt is not an object")
        result[spec.job_id] = value
    return result


def _validated_rows_v1(
    plan: CampaignPlanV1,
    specs: Sequence[SeedJobSpecV1],
    results: Mapping[str, Mapping[str, Any]],
    *,
    expected_identities: Mapping[str, Mapping[str, Any]],
) -> list[JSONMap]:
    if set(results) != {spec.job_id for spec in specs}:
        raise ValueError("seed receipt identities differ from the frozen plan")
    rows: list[JSONMap] = []
    for spec in specs:
        value = results[spec.job_id]
        expected_identity = expected_identities.get(spec.job_id)
        if expected_identity is None or {
            field: value.get(field) for field in expected_identity
        } != expected_identity:
            raise ValueError(f"{spec.job_id} has a stale shard identity")
        if value.get("phase") != plan.phase:
            raise ValueError(f"{spec.job_id} reports a different phase")
        if value.get("campaign_id") != plan.campaign_id:
            raise ValueError(f"{spec.job_id} reports a different campaign")
        executed = value.get("executed_seed_pairs")
        expected = [
            {"simulator_seed": a, "teammate_seed": b}
            for a, b in spec.seed_pairs
        ]
        if executed != expected:
            raise ValueError(f"{spec.job_id} executed a different seed")
        shard_rows = value.get("endpoint_rows")
        if not isinstance(shard_rows, list) or len(shard_rows) != plan.lane_count:
            raise ValueError(f"{spec.job_id} has the wrong lane count")
        summaries = value.get("runtime_summaries")
        if not isinstance(summaries, list) or len(summaries) != plan.lane_count:
            raise ValueError(f"{spec.job_id} lacks compact runtime summaries")
        if value.get("contracts", {}).get("compact_runtime_summaries") is not True:
            raise ValueError(f"{spec.job_id} did not compact runtime summaries")
        forbidden = {"accepted_execution_trace", "runtime_audit"}
        if any(forbidden & set(summary) for summary in summaries):
            raise ValueError(f"{spec.job_id} retained bulky duplicate traces")
        rows.extend(dict(row) for row in shard_rows)
    return rows


def _confirmation_attributions_v1(
    specs: Sequence[SeedJobSpecV1],
    results: Mapping[str, Mapping[str, Any]],
) -> list[JSONMap]:
    values: list[JSONMap] = []
    for spec in specs:
        rows = results[spec.job_id].get("action_attributions")
        if not isinstance(rows, list) or len(rows) != 3:
            raise ValueError(
                f"{spec.job_id} lacks the three searched-lane attributions"
            )
        values.extend(dict(row) for row in rows)
    return values


def _merge_selection_v1(
    panel: Mapping[str, Any],
    specs: Sequence[SeedJobSpecV1],
    results: Mapping[str, Mapping[str, Any]],
    expected_identities: Mapping[str, Mapping[str, Any]],
) -> JSONMap:
    from o2o_dps.offline_wave_d5_selection_v1 import select_d5_candidate_v1

    rows = _validated_rows_v1(
        SELECTION_PLAN,
        specs,
        results,
        expected_identities=expected_identities,
    )
    receipt = select_d5_candidate_v1(
        contract=panel["selection_contract"],
        selection_rows=rows,
    )
    if not isinstance(receipt, Mapping) or not isinstance(
        receipt.get("frozen_candidate"), Mapping
    ):
        raise RuntimeError("D5 selection did not freeze one candidate")
    return {
        "schema": f"{SCHEMA}/selection",
        "status": "D5_SELECTION_COMPLETE",
        "implementation_revision": IMPLEMENTATION_REVISION,
        "campaign_id": SELECTION_PLAN.campaign_id,
        "candidate_panel_sha256": next(iter(expected_identities.values()))[
            "candidate_panel_sha256"
        ],
        "candidate_panel": panel,
        "selection_receipt": dict(receipt),
        "endpoint_rows": rows,
        "contracts": {
            "one_seed_per_process": True,
            "candidate_panel_frozen_before_seeds": True,
            "all_seed_terminal_endpoint": True,
            "confirmation_rows_consumed": 0,
            "compact_runtime_summaries": True,
        },
    }


def _merge_confirmation_v1(
    selection: Mapping[str, Any],
    specs: Sequence[SeedJobSpecV1],
    results: Mapping[str, Mapping[str, Any]],
    expected_identities: Mapping[str, Mapping[str, Any]],
) -> JSONMap:
    from o2o_dps.offline_wave_d5_selection_v1 import (
        adjudicate_d5_confirmation_v1,
    )

    rows = _validated_rows_v1(
        CONFIRMATION_PLAN,
        specs,
        results,
        expected_identities=expected_identities,
    )
    receipt = adjudicate_d5_confirmation_v1(
        selection_contract=selection["candidate_panel"]["selection_contract"],
        selection_receipt=selection["selection_receipt"],
        confirmation_endpoint_rows=rows,
        confirmation_action_attributions=_confirmation_attributions_v1(
            specs, results
        ),
    )
    return {
        "schema": f"{SCHEMA}/confirmation",
        "status": "D5_FRESH_CONFIRMATION_COMPLETE",
        "implementation_revision": IMPLEMENTATION_REVISION,
        "campaign_id": CONFIRMATION_PLAN.campaign_id,
        "candidate_panel_sha256": next(iter(expected_identities.values()))[
            "candidate_panel_sha256"
        ],
        "selection_receipt_sha256": next(iter(expected_identities.values()))[
            "selection_receipt_sha256"
        ],
        "selection_receipt": selection["selection_receipt"],
        "confirmation_receipt": receipt,
        "endpoint_rows": rows,
        "contracts": {
            "one_seed_per_process": True,
            "winner_frozen_before_confirmation": True,
            "confirmation_cannot_reselect": True,
            "all_seed_terminal_endpoint": True,
            "compact_runtime_summaries": True,
        },
    }


def _execute_plan_v1(
    scheduler: Any,
    plan: CampaignPlanV1,
    *,
    resume: bool,
    candidate_panel_sha256: str,
    selection_receipt_sha256: str | None,
    d4_source: Mapping[str, Any],
) -> tuple[
    tuple[SeedJobSpecV1, ...],
    dict[str, Mapping[str, Any]],
    dict[str, JSONMap],
]:
    specs = build_seed_job_specs_v1(plan)
    expected_identities = {
        spec.job_id: _expected_shard_identity_v1(
            plan,
            spec,
            candidate_panel_sha256=candidate_panel_sha256,
            selection_receipt_sha256=selection_receipt_sha256,
            d4_source=d4_source,
        )
        for spec in specs
    }
    _prepare_remote_dir_v1(scheduler, plan, resume=resume)
    if resume:
        _resume_preflight_v1(scheduler, plan, specs, expected_identities)
    by_node = {
        node: tuple(spec for spec in specs if spec.node == node) for node in NODES
    }
    with ThreadPoolExecutor(max_workers=len(NODES)) as pool:
        futures = {
            pool.submit(
                _run_node_batch_v1,
                scheduler,
                plan,
                node,
                node_specs,
                candidate_panel_sha256=candidate_panel_sha256,
                selection_receipt_sha256=selection_receipt_sha256,
            ): node
            for node, node_specs in by_node.items()
            if node_specs
        }
        for future in as_completed(futures):
            future.result()
    return (
        specs,
        _fetch_seed_results_v1(scheduler, plan, specs),
        expected_identities,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase",
        choices=("selection", "confirmation", "full"),
        default="full",
    )
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    phases = (
        ("selection", "confirmation") if args.phase == "full" else (args.phase,)
    )
    if args.plan_only:
        print(json.dumps(plan_receipt_v1(phases), ensure_ascii=False))
        return 0

    sys.path.insert(0, str(Path.home() / "mine_code/scheduleurm/skill"))
    import scheduler

    d4_source = json.loads(D4_LOCAL.read_text(encoding="utf-8"))
    if not isinstance(d4_source, Mapping):
        raise ValueError("D4 source must be an object")

    if "selection" in phases:
        panel = _write_candidate_panel_v1()
        panel_sha256 = _candidate_panel_sha256_v1(panel)
        _stage_small_inputs_v1(scheduler, include_selection_receipt=False)
        specs, results, expected_identities = _execute_plan_v1(
            scheduler,
            SELECTION_PLAN,
            resume=args.resume,
            candidate_panel_sha256=panel_sha256,
            selection_receipt_sha256=None,
            d4_source=d4_source,
        )
        selection = _merge_selection_v1(
            panel, specs, results, expected_identities
        )
        SELECTION_RECEIPT_LOCAL.parent.mkdir(parents=True, exist_ok=True)
        SELECTION_RECEIPT_LOCAL.write_text(
            json.dumps(selection, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    else:
        selection = json.loads(
            SELECTION_RECEIPT_LOCAL.read_text(encoding="utf-8")
        )

    if "confirmation" in phases:
        panel_authority = _selection_panel_authority_v1(selection)
        panel_sha256 = _candidate_panel_sha256_v1(panel_authority)
        selection_sha256 = _selection_receipt_sha256_v1(selection)
        _stage_small_inputs_v1(scheduler, include_selection_receipt=True)
        specs, results, expected_identities = _execute_plan_v1(
            scheduler,
            CONFIRMATION_PLAN,
            resume=args.resume,
            candidate_panel_sha256=panel_sha256,
            selection_receipt_sha256=selection_sha256,
            d4_source=d4_source,
        )
        confirmation = _merge_confirmation_v1(
            selection, specs, results, expected_identities
        )
        CONFIRMATION_OUTPUT_LOCAL.parent.mkdir(parents=True, exist_ok=True)
        CONFIRMATION_OUTPUT_LOCAL.write_text(
            json.dumps(confirmation, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "selection_output": str(SELECTION_RECEIPT_LOCAL),
                    "confirmation_output": str(CONFIRMATION_OUTPUT_LOCAL),
                    "status": confirmation["status"],
                    "confirmation_receipt": confirmation[
                        "confirmation_receipt"
                    ],
                },
                ensure_ascii=False,
            )
        )
    else:
        print(
            json.dumps(
                {
                    "selection_output": str(SELECTION_RECEIPT_LOCAL),
                    "status": selection["status"],
                    "selection_receipt": selection["selection_receipt"],
                },
                ensure_ascii=False,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
