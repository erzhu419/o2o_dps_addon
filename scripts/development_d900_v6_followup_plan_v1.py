"""Print, but never run, the two d900 checks after a new v6 model is reduced."""

from __future__ import annotations

import argparse
import json
from pathlib import PurePosixPath
import shlex


BASE = "/home/zhengliang01/scheduleurm_work/o2o-dps-hpc"
STAGE5 = (
    f"{BASE}/offline_data/derived/chronicle_external_team_wave_model/v2/"
    "utk_postfix_dev_20260903_noon/"
    "d900a97b-b53e-4444-943b-3e0f2be8d477."
    "7b5a910c5b70011e5a9a9767a8c007abca7b4850dcc44fcf5032f7a3582e2058.jsonl.gz"
)
WAVE = "02829cd0-85c3-4b6f-adba-059398e6ae14:external-v2-wave:1"
COMPONENT = "114175c6c5af0b03978551442d51dcbbf325f8ba2af8b7e3bef468bc7da8fa68"
STAGE5_SHA = "75fe0c215d09b6b0ca5e3a2c46b5397f70b4b5de60f8740dceae1b2465576bc0"
PARTITION_SHA = "20ec42bf166a17f2102325f1abe7bded5c5d6062f5f49d515422cff1e05eade9"
PYTHON = str(PurePosixPath(BASE).parent / "conda_envs/scomp-py310/bin/python3.10")
OLD_RESULT_SHA = "1389ce06506b89d7c2040befdf24320bb0eb557d798dded4b0edb9dfa935bfe4"
OLD_MODEL_SHA = "dc4022d86393823d8df67f890805348438126735e73c5d40234cd915e45da7f2"


def build_plan_v1(*, code_root: str, simulator_root: str, frozen_dispatch: str,
                  runtime_store: str, result_sha: str, model_sha: str,
                  metadata: str | None = None, exact_build: str | None = None,
                  deployed_binding: str | None = None, bridge: str | None = None) -> dict:
    """Only construct commands; caller stages sources and explicitly runs later."""
    if result_sha == OLD_RESULT_SHA or model_sha == OLD_MODEL_SHA:
        raise ValueError("old v4 reducer/model identity cannot score the new v6 head")
    root = PurePosixPath(code_root)
    metadata = metadata or (
        f"{BASE}/offline_data/chronicle_raw/external_api/v1/objects/sha256/15/"
        "15f5ad6e19894101ce464fbef69d98c7125ff2c1c973d8478ecc6f18381b88c9.json"
    )
    exact_build = exact_build or str(root / "results/responsive-team-v4/v34-doomguard-exact-fury-build-request.json")
    deployed_binding = deployed_binding or str(root / "results/responsive-team-v4/deployed-contra-runtime-binding-v1.951b8faa.json")
    bridge = bridge or str(root / "bin/o2obridge.seedfix-v26.observed-damage-v2.withdb.goamd64v1.linux-amd64")
    score = [
        PYTHON, str(root / "scripts/development_d900_target_head_model_eval_v1.py"),
        "--stage5", STAGE5, "--runtime-store", runtime_store,
        "--result-sha", result_sha, "--model-sha", model_sha,
    ]
    wave = [
        PYTHON, str(root / "scripts/development_responsive_upper_kara_trash_full_wave_v1.py"),
        "--metadata", metadata,
        "--stage5", STAGE5,
        "--exact-build", exact_build,
        "--frozen-dispatch", frozen_dispatch,
        "--runtime-store", runtime_store,
        "--bridge", bridge,
        "--simulator-root", simulator_root,
        "--component-id", COMPONENT,
        "--stage5-content-sha256", STAGE5_SHA,
        "--partition-compressed-file-sha256", PARTITION_SHA,
        "--result-sha256", result_sha,
        "--model-sha256", model_sha,
        "--wave-id", WAVE,
        "--simulator-seed", "2026092001",
        "--teammate-seed", "2026092002",
        "--seed-count", "1",
        "--source", "all",
        "--route-focus",
        "--attackability-mode", "OBSERVED_ONSET_UNTIL_SIM_DEATH",
        "--deployed-runtime-binding", deployed_binding,
    ]
    return {
        "schema": "development_d900_v6_followup_plan/v1",
        "status": "COMMANDS_ONLY_NOT_EXECUTED",
        "comparison_authorized": False,
        "node": "node004",
        "order": ["heldout_target_choice_small_score", "one_seed_three_baseline_full_wave"],
        "score": {"argv": score, "shell_command": shlex.join(score)},
        "full_wave": {"argv": wave, "shell_command": shlex.join(wave)},
        "expected_source_policy_ids": [
            "cat.fury.profile1", "contra260817.fury.source_candidate", "contra.deployed.fury.raid_b"
        ],
        "source_files_to_stage_before_execution": [
            "o2o_dps/chronicle_external_teammate_response_model_v1.py",
            "o2o_dps/responsive_team_runtime_store_v1.py",
            "o2o_dps/responsive_team_hpc_result_loader_v1.py",
            "o2o_dps/responsive_team_bridge_adapter_v1.py",
            "o2o_dps/responsive_incantagos_driven_bridge_v1.py",
            "o2o_dps/policy_observation_causal_projection_v1.py",
            "o2o_dps/sim_bridge_dynamic_v4.py",
            "o2o_dps/upper_kara_development_route_focus_v1.py",
            "o2o_dps/upper_kara_trash_dynamic_v4_adapter_v1.py",
            "o2o_dps/upper_kara_target_head_heldout_eval_v1.py",
            "scripts/development_d900_target_head_model_eval_v1.py",
            "scripts/development_responsive_upper_kara_trash_full_wave_v1.py",
            "configs/experts/contra260817_source_manifest_91baa120.json",
        ],
        "source_runtime_note": (
            "Use a new v6 reduced store and refreshed source/bridge closure. "
            "Make the Contra_new source tree available to its exact identity binder. "
            "Preserve the v49 historical build, seed, route, and attackability settings. "
            "The historical route and HP/armor assumptions remain development-only."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code-root", required=True)
    parser.add_argument("--simulator-root", required=True)
    parser.add_argument("--frozen-dispatch", required=True)
    parser.add_argument("--runtime-store", required=True)
    parser.add_argument("--result-sha", required=True)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--metadata")
    parser.add_argument("--exact-build")
    parser.add_argument("--deployed-binding")
    parser.add_argument("--bridge")
    args = parser.parse_args()
    print(json.dumps(build_plan_v1(**vars(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
