"""Run the frozen D3 winner on 48 blind seed pairs across node003-node006."""

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

from o2o_dps.offline_wave_d3_frozen_heldout_v1 import (
    PI_STAR,
    adjudicate_frozen_heldout_expansion_v1,
    build_frozen_heldout_expansion_contract_v1,
)
from scripts.development_offline_wave_policy_d900_d2_remote_v1 import (
    BASE,
    RUNTIME,
    _d2_argv,
)
from scripts.development_offline_wave_policy_d900_d3_frozen_heldout_v1 import (
    OUTPUT_SCHEMA as SHARD_OUTPUT_SCHEMA,
)


SCHEMA = "development_offline_wave_policy_d900_d3_frozen_blind_multinode/v1"
NODES = ("node003", "node004", "node005", "node006")
FIRST_SEED = 2026101001
SEEDS_PER_NODE = 12
WORKERS_PER_SEED = 5
MAX_DECISIONS = 300
EXPANSION_ID = "d900-d3-frozen-blind-48seed-v1"

FROZEN_LOCAL = (
    ROOT
    / "results/offline-wave-policy-v1/d900-d3-tail-repair-smoke-v1.json"
)
FROZEN_REMOTE = (
    f"{RUNTIME}/results/offline-wave-policy-v1/"
    "d900-d3-tail-repair-smoke-v1.json"
)
REMOTE_RESULT_DIR = (
    f"{RUNTIME}/results/offline-wave-policy-v1/{EXPANSION_ID}"
)
OUTPUT = (
    ROOT
    / "results/offline-wave-policy-v1/"
    "d900-d3-frozen-blind-48seed-v1.json"
)

# The simulator, model, Stage5 and Contra sources are already shared by all
# four nodes.  Only this small changed source surface and the frozen receipt are
# staged; no offline-data or simulator payload is copied.
SOURCE_FILES = (
    "o2o_dps/offline_wave_searched_runtime_v1.py",
    "o2o_dps/offline_wave_d3_action_attribution_v1.py",
    "o2o_dps/offline_wave_d3_frozen_heldout_v1.py",
    "scripts/development_offline_wave_policy_d900_d3_v1.py",
    "scripts/development_offline_wave_policy_d900_d3_frozen_heldout_v1.py",
)


@dataclass(frozen=True)
class SeedJobSpecV1:
    job_id: str
    node: str
    first_seed: int
    seed_count: int = 1
    lane_workers: int = WORKERS_PER_SEED

    @property
    def seed_pairs(self) -> tuple[tuple[int, int], ...]:
        return tuple(
            (seed, seed + 100_000)
            for seed in range(self.first_seed, self.first_seed + self.seed_count)
        )


def build_seed_job_specs_v1() -> tuple[SeedJobSpecV1, ...]:
    return tuple(
        SeedJobSpecV1(
            job_id=f"seed-{FIRST_SEED + node_index * SEEDS_PER_NODE + offset}",
            node=node,
            first_seed=FIRST_SEED + node_index * SEEDS_PER_NODE + offset,
        )
        for node_index, node in enumerate(NODES)
        for offset in range(SEEDS_PER_NODE)
    )


def _drop_option(argv: list[str], option: str) -> None:
    if option not in argv:
        return
    index = argv.index(option)
    if index + 1 >= len(argv):
        raise ValueError(f"{option} lacks a value")
    del argv[index : index + 2]


def build_seed_job_argv_v1(spec: SeedJobSpecV1) -> list[str]:
    argv = _d2_argv()
    argv[1] = (
        f"{RUNTIME}/scripts/"
        "development_offline_wave_policy_d900_d3_frozen_heldout_v1.py"
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
            "--expansion-id",
            f"{EXPANSION_ID}/{spec.job_id}",
            "--heldout-seed",
            str(spec.first_seed),
            "--heldout-seed-count",
            str(spec.seed_count),
            "--max-decisions",
            str(MAX_DECISIONS),
            "--lane-workers",
            str(spec.lane_workers),
        )
    )
    return argv


def _expected_pairs(specs: Sequence[SeedJobSpecV1]) -> tuple[tuple[int, int], ...]:
    return tuple(pair for spec in specs for pair in spec.seed_pairs)


def merge_seed_job_results_v1(
    *,
    frozen_source: Mapping[str, Any],
    specs: Sequence[SeedJobSpecV1],
    seed_job_results: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Validate 48 checkpointed seed jobs and adjudicate one paired panel."""

    if tuple(specs) != build_seed_job_specs_v1():
        raise ValueError("seed jobs differ from the frozen four-node plan")
    expected_ids = {spec.job_id for spec in specs}
    if set(seed_job_results) != expected_ids:
        raise ValueError("seed-job result identities differ from the frozen plan")
    pairs = _expected_pairs(specs)
    if len(pairs) != len(set(pairs)):
        raise ValueError("frozen shard plan repeats a seed pair")
    contract = build_frozen_heldout_expansion_contract_v1(
        source=frozen_source,
        heldout_seed_pairs=pairs,
        expansion_id=EXPANSION_ID,
    )

    rows: list[dict[str, Any]] = []
    job_summaries: list[dict[str, Any]] = []
    identity_fields = (
        "frozen_source_binding",
        "request_sha256",
        "dynamic_config_sha256",
        "evaluation_build_ref",
        "target_rule_id",
    )
    reference_identity: dict[str, Any] | None = None
    for spec in specs:
        value = seed_job_results[spec.job_id]
        if value.get("schema") != SHARD_OUTPUT_SCHEMA:
            raise ValueError(f"{spec.job_id} has the wrong output schema")
        shard_pairs = tuple(
            (
                int(row["simulator_seed"]),
                int(row["teammate_seed"]),
            )
            for row in value.get("contract", {}).get("heldout_seed_pairs", [])
        )
        if shard_pairs != spec.seed_pairs:
            raise ValueError(f"{spec.job_id} evaluated the wrong seed pairs")
        expected_shard_contract = build_frozen_heldout_expansion_contract_v1(
            source=frozen_source,
            heldout_seed_pairs=spec.seed_pairs,
            expansion_id=f"{EXPANSION_ID}/{spec.job_id}",
        )
        if value.get("contract") != expected_shard_contract:
            raise ValueError(
                f"{spec.job_id} checkpoint does not contain the exact frozen "
                "shard contract"
            )
        current_identity = {field: value.get(field) for field in identity_fields}
        if reference_identity is None:
            reference_identity = current_identity
        elif current_identity != reference_identity:
            raise ValueError("shards used different frozen environment identities")
        shard_rows = value.get("runtime_rows")
        if not isinstance(shard_rows, list):
            raise ValueError(f"{spec.job_id} lacks runtime rows")
        rows.extend(dict(row) for row in shard_rows)
        job_summaries.append(
            {
                "job_id": spec.job_id,
                "node": spec.node,
                "first_seed": spec.first_seed,
                "seed_count": spec.seed_count,
                "lane_workers": spec.lane_workers,
                "status": value.get("status"),
                "valid_paired_seed_count": value.get("receipt", {}).get(
                    "valid_paired_seed_count"
                ),
            }
        )

    receipt = adjudicate_frozen_heldout_expansion_v1(
        contract=contract,
        rows=rows,
    )
    pi_star_attribution = [
        {
            "simulator_seed": row["simulator_seed"],
            "teammate_seed": row["teammate_seed"],
            "attribution": row.get("searched_action_attribution"),
        }
        for row in rows
        if row.get("controller_id") == PI_STAR
    ]
    pi_star_attribution.sort(
        key=lambda row: (row["simulator_seed"], row["teammate_seed"])
    )
    return {
        "schema": SCHEMA,
        "status": receipt["status"],
        "contract": contract,
        "receipt": receipt,
        "runtime_rows": rows,
        "pi_star_action_attribution_by_seed": pi_star_attribution,
        "seed_jobs": job_summaries,
        **(reference_identity or {}),
        "contracts": {
            "winner_loaded_verbatim_from_prior_d3_result": True,
            "candidate_generation_performed": False,
            "selection_performed": False,
            "one_process_per_seed": True,
            "twelve_seed_processes_per_node": True,
            "all_seed_components_new_vs_prior_d3": True,
            "same_seed_pair_for_all_five_controllers": True,
            "incomplete_imputed_as_zero": False,
            "pi_d_and_pi_star_fallback_required_zero": True,
            "large_data_transferred": False,
            "comparison_authorized": False,
            "deployment_authorized": False,
        },
    }


def _stage_small_inputs_v1(scheduler: Any) -> None:
    mkdir_command = shlex.join(
        [
            "mkdir",
            "-p",
            f"{RUNTIME}/o2o_dps",
            f"{RUNTIME}/scripts",
            str(Path(FROZEN_REMOTE).parent),
            REMOTE_RESULT_DIR,
        ]
    )
    return_code, _stdout, stderr = scheduler.run_on(
        NODES[0], mkdir_command, timeout=30, check=False
    )
    if return_code:
        raise RuntimeError(f"could not prepare shared D3 runtime: {stderr[-2000:]}")
    transfers = tuple((ROOT / relative, f"{RUNTIME}/{relative}") for relative in SOURCE_FILES)
    transfers += ((FROZEN_LOCAL, FROZEN_REMOTE),)
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


def _seed_result_path(spec: SeedJobSpecV1) -> str:
    return f"{REMOTE_RESULT_DIR}/{spec.job_id}.json"


def build_node_batch_command_v1(specs: Sequence[SeedJobSpecV1]) -> str:
    if not specs or len({spec.node for spec in specs}) != 1:
        raise ValueError("one node batch requires at least one job on one node")
    launches: list[str] = ["pids=''", "status=0"]
    logs: list[str] = []
    for spec in specs:
        remote_output = _seed_result_path(spec)
        temporary_output = remote_output + ".tmp"
        log_output = f"{REMOTE_RESULT_DIR}/{spec.job_id}.log"
        python_command = (
            f"PYTHONDONTWRITEBYTECODE=1 PYTHONPATH={shlex.quote(RUNTIME)} "
            + shlex.join(build_seed_job_argv_v1(spec))
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


def _run_node_batch_v1(
    scheduler: Any,
    node: str,
    specs: Sequence[SeedJobSpecV1],
) -> None:
    command = build_node_batch_command_v1(specs)
    for attempt in range(3):
        return_code, stdout, stderr = scheduler.run_on(
            node,
            command,
            timeout=1800,
            check=False,
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
    specs: Sequence[SeedJobSpecV1],
) -> dict[str, Mapping[str, Any]]:
    command = shlex.join(["cat", *(_seed_result_path(spec) for spec in specs)])
    return_code, stdout, stderr = scheduler.run_on(
        NODES[0],
        command,
        timeout=120,
        check=False,
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
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()
    specs = build_seed_job_specs_v1()
    if args.plan_only:
        print(
            json.dumps(
                {
                    "schema": SCHEMA + "/plan",
                    "expansion_id": EXPANSION_ID,
                    "total_seed_pair_count": len(_expected_pairs(specs)),
                    "total_replay_count": len(_expected_pairs(specs)) * 5,
                    "one_process_per_seed": True,
                    "seed_jobs": [
                        {
                            "job_id": spec.job_id,
                            "node": spec.node,
                            "first_seed": spec.first_seed,
                            "seed_count": spec.seed_count,
                            "lane_workers": spec.lane_workers,
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

    _stage_small_inputs_v1(scheduler)
    specs_by_node = {
        node: tuple(spec for spec in specs if spec.node == node)
        for node in NODES
    }
    with ThreadPoolExecutor(max_workers=len(NODES)) as pool:
        futures = {
            pool.submit(_run_node_batch_v1, scheduler, node, node_specs): node
            for node, node_specs in specs_by_node.items()
        }
        for future in as_completed(futures):
            future.result()
    results = _fetch_seed_results_v1(scheduler, specs)

    frozen_source = json.loads(FROZEN_LOCAL.read_text(encoding="utf-8"))
    merged = merge_seed_job_results_v1(
        frozen_source=frozen_source,
        specs=specs,
        seed_job_results=results,
    )
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(OUTPUT),
                "bytes": OUTPUT.stat().st_size,
                "status": merged["status"],
                "requested_pair_count": merged["receipt"][
                    "requested_pair_count"
                ],
                "valid_paired_seed_count": merged["receipt"][
                    "valid_paired_seed_count"
                ],
                "aggregate_on_valid_pairs_only": merged["receipt"][
                    "aggregate_on_valid_pairs_only"
                ],
                "seed_jobs": merged["seed_jobs"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
