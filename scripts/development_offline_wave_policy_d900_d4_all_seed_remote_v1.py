"""Run D4 all-seed endpoints as one seed/process on node003-node006."""

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

from o2o_dps.offline_wave_d3_frozen_heldout_v1 import PI_STAR
from o2o_dps.offline_wave_d4_all_seed_endpoint_v1 import (
    DIAGNOSTIC_REUSE_SEEN,
    FRESH_CONFIRMATORY,
    adjudicate_all_seed_endpoint_v1,
    build_all_seed_endpoint_contract_v1,
)
from scripts.development_offline_wave_policy_d900_d2_remote_v1 import (
    RUNTIME,
    _d2_argv,
)
from scripts.development_offline_wave_policy_d900_d3_frozen_heldout_remote_v1 import (
    FROZEN_LOCAL,
    FROZEN_REMOTE,
)
from scripts.development_offline_wave_policy_d900_d4_all_seed_v1 import (
    OUTPUT_SCHEMA as SHARD_OUTPUT_SCHEMA,
)


JSONMap = dict[str, Any]
SCHEMA = "development_offline_wave_policy_d900_d4_all_seed_multinode/v1"
NODES = ("node003", "node004", "node005", "node006")
WORKERS_PER_SEED = 5
MAX_DECISIONS = 300
FIXED_HORIZON_MS = 15_531
PRIOR_LOCAL = (
    ROOT
    / "results/offline-wave-policy-v1/"
    "d900-d3-frozen-blind-48seed-v1.json"
)
PRIOR_REMOTE = (
    f"{RUNTIME}/results/offline-wave-policy-v1/"
    "d900-d3-frozen-blind-48seed-v1.json"
)

SOURCE_FILES = (
    "o2o_dps/offline_wave_searched_runtime_v1.py",
    "o2o_dps/offline_wave_d3_action_attribution_v1.py",
    "o2o_dps/offline_wave_d3_frozen_heldout_v1.py",
    "o2o_dps/offline_wave_d4_all_seed_endpoint_v1.py",
    "scripts/development_offline_wave_policy_d900_d3_v1.py",
    "scripts/development_offline_wave_policy_d900_d3_frozen_heldout_v1.py",
    "scripts/development_offline_wave_policy_d900_d4_all_seed_v1.py",
)


@dataclass(frozen=True)
class CampaignPlanV1:
    name: str
    campaign_id: str
    evidence_mode: str
    first_seed: int
    seed_count: int
    expansion_id: str

    @property
    def seed_pairs(self) -> tuple[tuple[int, int], ...]:
        return tuple(
            (seed, seed + 100_000)
            for seed in range(self.first_seed, self.first_seed + self.seed_count)
        )

    @property
    def remote_result_dir(self) -> str:
        return f"{RUNTIME}/results/offline-wave-policy-v1/{self.expansion_id}"

    @property
    def output(self) -> Path:
        return (
            ROOT
            / "results/offline-wave-policy-v1"
            / f"{self.expansion_id}.json"
        )


DIAGNOSTIC_PLAN = CampaignPlanV1(
    name="diagnostic",
    campaign_id="d900-d4-seen-endpoint-diagnostic-v1",
    evidence_mode=DIAGNOSTIC_REUSE_SEEN,
    first_seed=2026101001,
    seed_count=7,
    expansion_id="d900-d4-seen-endpoint-diagnostic-7seed-v1",
)
CONFIRMATORY_PLAN = CampaignPlanV1(
    name="confirmatory",
    campaign_id="d900-d4-fresh-all-seed-confirmation-v1",
    evidence_mode=FRESH_CONFIRMATORY,
    first_seed=2026101049,
    seed_count=48,
    expansion_id="d900-d4-fresh-all-seed-2026101049-2026101096-v1",
)
PLANS = {row.name: row for row in (DIAGNOSTIC_PLAN, CONFIRMATORY_PLAN)}


@dataclass(frozen=True)
class SeedJobSpecV1:
    job_id: str
    node: str
    first_seed: int
    lane_workers: int = WORKERS_PER_SEED

    @property
    def seed_pairs(self) -> tuple[tuple[int, int], ...]:
        return ((self.first_seed, self.first_seed + 100_000),)


def build_seed_job_specs_v1(plan: CampaignPlanV1) -> tuple[SeedJobSpecV1, ...]:
    specs = tuple(
        SeedJobSpecV1(
            job_id=f"seed-{seed}",
            node=NODES[offset % len(NODES)],
            first_seed=seed,
        )
        for offset, seed in enumerate(
            range(plan.first_seed, plan.first_seed + plan.seed_count)
        )
    )
    if tuple(pair for spec in specs for pair in spec.seed_pairs) != plan.seed_pairs:
        raise RuntimeError("one-seed shard plan differs from campaign seed order")
    return specs


def _drop_option(argv: list[str], option: str) -> None:
    if option not in argv:
        return
    index = argv.index(option)
    if index + 1 >= len(argv):
        raise ValueError(f"{option} lacks a value")
    del argv[index : index + 2]


def build_seed_job_argv_v1(
    plan: CampaignPlanV1, spec: SeedJobSpecV1
) -> list[str]:
    argv = _d2_argv()
    argv[1] = (
        f"{RUNTIME}/scripts/"
        "development_offline_wave_policy_d900_d4_all_seed_v1.py"
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
            "--frozen-d3-result",
            FROZEN_REMOTE,
            "--prior-frozen-expansion-result",
            PRIOR_REMOTE,
            "--expansion-id",
            f"{plan.expansion_id}/{spec.job_id}",
            "--endpoint-campaign-id",
            plan.campaign_id,
            "--campaign-first-seed",
            str(plan.first_seed),
            "--campaign-seed-count",
            str(plan.seed_count),
            "--evidence-mode",
            plan.evidence_mode,
            "--heldout-seed",
            str(spec.first_seed),
            "--heldout-seed-count",
            "1",
            "--max-decisions",
            str(MAX_DECISIONS),
            "--lane-workers",
            str(spec.lane_workers),
        )
    )
    return argv


def merge_seed_job_results_v1(
    *,
    plan: CampaignPlanV1,
    frozen_source: Mapping[str, Any],
    prior_expansion_source: Mapping[str, Any],
    specs: Sequence[SeedJobSpecV1],
    seed_job_results: Mapping[str, Mapping[str, Any]],
) -> JSONMap:
    expected_specs = build_seed_job_specs_v1(plan)
    if tuple(specs) != expected_specs:
        raise ValueError("seed jobs differ from the frozen one-seed shard plan")
    if set(seed_job_results) != {spec.job_id for spec in specs}:
        raise ValueError("seed-job result identities differ from the frozen plan")
    contract = build_all_seed_endpoint_contract_v1(
        frozen_d3_source=frozen_source,
        prior_expansion_source=prior_expansion_source,
        seed_pairs=plan.seed_pairs,
        campaign_id=plan.campaign_id,
        horizon_ms=FIXED_HORIZON_MS,
        evidence_mode=plan.evidence_mode,
    )
    frozen_identity = {
        field: frozen_source.get(field)
        for field in (
            "frozen_source_binding",
            "request_sha256",
            "dynamic_config_sha256",
            "evaluation_build_ref",
            "target_rule_id",
        )
    }
    if any(value is None for value in frozen_identity.values()):
        raise ValueError("frozen D3 source lacks a required environment identity")
    endpoint_rows: list[JSONMap] = []
    pi_star_attribution: list[JSONMap] = []
    job_summaries: list[JSONMap] = []
    for spec in specs:
        value = seed_job_results[spec.job_id]
        if value.get("schema") != SHARD_OUTPUT_SCHEMA:
            raise ValueError(f"{spec.job_id} has the wrong output schema")
        if value.get("contract") != contract:
            raise ValueError(
                f"{spec.job_id} does not contain the exact global endpoint contract"
            )
        executed_rows = value.get("executed_seed_pairs")
        if not isinstance(executed_rows, list):
            raise ValueError(f"{spec.job_id} lacks executed_seed_pairs")
        executed_pairs = tuple(
            (int(row["simulator_seed"]), int(row["teammate_seed"]))
            for row in executed_rows
        )
        if executed_pairs != spec.seed_pairs:
            raise ValueError(f"{spec.job_id} executed the wrong seed pair")
        current_identity = {
            field: value.get(field) for field in frozen_identity
        }
        if current_identity != frozen_identity:
            raise ValueError(
                f"{spec.job_id} environment identity differs from the frozen source"
            )
        if value.get("fixed_horizon_ms") != FIXED_HORIZON_MS:
            raise ValueError(f"{spec.job_id} used the wrong fixed horizon")
        if value.get("required_target_indexes") != [0, 1, 2]:
            raise ValueError(f"{spec.job_id} used the wrong required target registry")
        shard_rows = value.get("endpoint_rows")
        if not isinstance(shard_rows, list) or len(shard_rows) != 5:
            raise ValueError(f"{spec.job_id} lacks five endpoint rows")
        endpoint_rows.extend(dict(row) for row in shard_rows)

        source_rows = value.get("source_runtime_rows")
        if not isinstance(source_rows, list) or len(source_rows) != 5:
            raise ValueError(f"{spec.job_id} lacks five source runtime rows")
        pi_source = [row for row in source_rows if row.get("controller_id") == PI_STAR]
        if len(pi_source) != 1:
            raise ValueError(f"{spec.job_id} lacks one pi-star source row")
        pi_star_attribution.append(
            {
                "simulator_seed": spec.seed_pairs[0][0],
                "teammate_seed": spec.seed_pairs[0][1],
                "attribution": pi_source[0].get("searched_action_attribution"),
            }
        )
        wall_values = [
            float(row["wall_seconds"])
            for row in source_rows
            if isinstance(row.get("wall_seconds"), (int, float))
            and not isinstance(row.get("wall_seconds"), bool)
        ]
        job_summaries.append(
            {
                "job_id": spec.job_id,
                "node": spec.node,
                "simulator_seed": spec.seed_pairs[0][0],
                "teammate_seed": spec.seed_pairs[0][1],
                "status": value.get("status"),
                "five_lane_critical_path_wall_seconds": (
                    max(wall_values) if len(wall_values) == 5 else None
                ),
            }
        )

    receipt = adjudicate_all_seed_endpoint_v1(
        contract=contract, rows=endpoint_rows
    )
    pi_star_attribution.sort(
        key=lambda row: (row["simulator_seed"], row["teammate_seed"])
    )
    return {
        "schema": SCHEMA,
        "status": receipt["status"],
        "contract": contract,
        "receipt": receipt,
        "endpoint_rows": endpoint_rows,
        "pi_star_action_attribution_by_seed": pi_star_attribution,
        "seed_jobs": job_summaries,
        **frozen_identity,
        "contracts": {
            "one_seed_per_process": True,
            "five_controller_lanes_per_seed": True,
            "four_remote_nodes": True,
            "new_result_directory_per_campaign": True,
            "incomplete_waves_retained": True,
            "large_data_transferred": False,
            "real_environment_comparison_authorized": False,
            "deployment_authorized": False,
        },
    }


def _seed_result_path(plan: CampaignPlanV1, spec: SeedJobSpecV1) -> str:
    return f"{plan.remote_result_dir}/{spec.job_id}.json"


def build_node_batch_command_v1(
    plan: CampaignPlanV1, specs: Sequence[SeedJobSpecV1]
) -> str:
    if not specs or len({spec.node for spec in specs}) != 1:
        raise ValueError("one node batch requires at least one job on one node")
    launches: list[str] = ["pids=''", "status=0"]
    logs: list[str] = []
    for spec in specs:
        remote_output = _seed_result_path(plan, spec)
        temporary_output = remote_output + ".tmp"
        log_output = f"{plan.remote_result_dir}/{spec.job_id}.log"
        python_command = (
            f"PYTHONDONTWRITEBYTECODE=1 PYTHONPATH={shlex.quote(RUNTIME)} "
            + shlex.join(build_seed_job_argv_v1(plan, spec))
        )
        launches.append(
            f"if test ! -s {shlex.quote(remote_output)}; then "
            f"({python_command} > {shlex.quote(temporary_output)} "
            f"2> {shlex.quote(log_output)}"
            f" && mv {shlex.quote(temporary_output)} "
            f"{shlex.quote(remote_output)}) & pids=\"$pids $!\"; fi"
        )
        logs.append(log_output)
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


def _stage_small_inputs_v1(
    scheduler: Any, plan: CampaignPlanV1, *, resume: bool
) -> None:
    if resume:
        prepare = shlex.join(["mkdir", "-p", plan.remote_result_dir])
    else:
        prepare = (
            f"test ! -e {shlex.quote(plan.remote_result_dir)} && "
            + shlex.join(["mkdir", "-p", plan.remote_result_dir])
        )
    return_code, _stdout, stderr = scheduler.run_on(
        NODES[0], prepare, timeout=30, check=False
    )
    if return_code:
        action = "resume" if resume else "create a new result directory"
        raise RuntimeError(
            f"could not {action} for {plan.expansion_id}: {stderr[-2000:]}"
        )
    transfers = tuple(
        (ROOT / relative, f"{RUNTIME}/{relative}") for relative in SOURCE_FILES
    )
    transfers += (
        (FROZEN_LOCAL, FROZEN_REMOTE),
        (PRIOR_LOCAL, PRIOR_REMOTE),
    )
    for source, destination in transfers:
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


def _run_node_batch_v1(
    scheduler: Any,
    plan: CampaignPlanV1,
    node: str,
    specs: Sequence[SeedJobSpecV1],
) -> None:
    command = build_node_batch_command_v1(plan, specs)
    for attempt in range(3):
        return_code, stdout, stderr = scheduler.run_on(
            node, command, timeout=1800, check=False
        )
        if return_code != 255 or attempt == 2:
            break
    if return_code:
        details = stderr[-8000:] if stderr.strip() else stdout[-8000:]
        raise RuntimeError(
            f"seed batch on {node} failed (rc={return_code}): {details}"
        )


def _fetch_seed_results_v1(
    scheduler: Any,
    plan: CampaignPlanV1,
    specs: Sequence[SeedJobSpecV1],
) -> dict[str, Mapping[str, Any]]:
    command = shlex.join(
        ["cat", *(_seed_result_path(plan, spec) for spec in specs)]
    )
    return_code, stdout, stderr = scheduler.run_on(
        NODES[0], command, timeout=120, check=False
    )
    if return_code:
        raise RuntimeError(f"could not fetch seed receipts: {stderr[-3000:]}")
    lines = [line for line in stdout.splitlines() if line.strip()]
    if len(lines) != len(specs):
        raise RuntimeError(
            f"expected {len(specs)} seed receipts, received {len(lines)}"
        )
    results: dict[str, Mapping[str, Any]] = {}
    for spec, line in zip(specs, lines):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                f"{spec.job_id} has a non-JSON checkpoint: {line[-1000:]}"
            ) from error
        if not isinstance(value, dict):
            raise RuntimeError(f"{spec.job_id} checkpoint is not a JSON object")
        results[spec.job_id] = value
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", choices=tuple(PLANS), required=True)
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    plan = PLANS[args.campaign]
    specs = build_seed_job_specs_v1(plan)
    if args.plan_only:
        print(
            json.dumps(
                {
                    "schema": SCHEMA + "/plan",
                    "campaign": plan.name,
                    "campaign_id": plan.campaign_id,
                    "expansion_id": plan.expansion_id,
                    "evidence_mode": plan.evidence_mode,
                    "first_seed": plan.first_seed,
                    "seed_count": plan.seed_count,
                    "one_seed_per_process": True,
                    "five_controller_lanes_per_process": True,
                    "jobs_by_node": {
                        node: sum(spec.node == node for spec in specs)
                        for node in NODES
                    },
                    "seed_jobs": [
                        {
                            "job_id": spec.job_id,
                            "node": spec.node,
                            "simulator_seed": spec.seed_pairs[0][0],
                            "teammate_seed": spec.seed_pairs[0][1],
                        }
                        for spec in specs
                    ],
                },
                ensure_ascii=False,
            )
        )
        return 0

    sys.path.insert(0, str(Path.home() / "mine_code/scheduleurm/skill"))
    import scheduler

    _stage_small_inputs_v1(scheduler, plan, resume=args.resume)
    specs_by_node = {
        node: tuple(spec for spec in specs if spec.node == node) for node in NODES
    }
    with ThreadPoolExecutor(max_workers=len(NODES)) as pool:
        futures = {
            pool.submit(
                _run_node_batch_v1, scheduler, plan, node, node_specs
            ): node
            for node, node_specs in specs_by_node.items()
            if node_specs
        }
        for future in as_completed(futures):
            future.result()
    results = _fetch_seed_results_v1(scheduler, plan, specs)
    frozen_source = json.loads(FROZEN_LOCAL.read_text(encoding="utf-8"))
    prior_source = json.loads(PRIOR_LOCAL.read_text(encoding="utf-8"))
    merged = merge_seed_job_results_v1(
        plan=plan,
        frozen_source=frozen_source,
        prior_expansion_source=prior_source,
        specs=specs,
        seed_job_results=results,
    )
    plan.output.parent.mkdir(parents=True, exist_ok=True)
    plan.output.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(plan.output),
                "bytes": plan.output.stat().st_size,
                "status": merged["status"],
                "requested_seed_count": merged["receipt"]["requested_seed_count"],
                "all_five_controllers_valid_seed_count": merged["receipt"][
                    "all_five_controllers_valid_seed_count"
                ],
                "development_model_superiority_status": merged["receipt"][
                    "development_model_superiority_status"
                ],
                "per_controller": merged["receipt"]["per_controller"],
                "pi_star_pairwise_by_baseline": merged["receipt"][
                    "pi_star_pairwise_by_baseline"
                ],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
