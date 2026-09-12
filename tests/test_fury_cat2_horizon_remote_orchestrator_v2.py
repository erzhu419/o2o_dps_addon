from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps.cat2new_fury_horizon_confirmation_v2 import (
    CONFIRMATION_ARM_IDS,
    CONFIRMATION_MASTER_SEEDS,
    build_horizon_confirmation_v2,
)
from o2o_dps.fury_cat2_horizon_confirmation_preparation_v2 import (
    _confirmation_manifest_v2,
)
from o2o_dps.fury_cat2_horizon_remote_orchestrator_v1 import (
    prepare_remote_attempt_v1,
)
from tests.test_cat2new_fury_horizon_confirmation_v1 import _source_plan


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "bin/o2obridge.seedfix-v10.withdb.goamd64v1.linux-amd64"
SITE = ROOT / "configs/hpc/site.local.json"


class FuryCat2HorizonRemoteOrchestratorV2Tests(unittest.TestCase):
    def test_v2_attempt_runs_one_analysis_owned_reduction_per_arm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = build_horizon_confirmation_v2(
                _source_plan(tuple(range(1, 257))),
                bridge_path=BRIDGE,
                bridge_platform="linux-amd64",
                bridge_build_id="seedfix-v10-test",
                workers_per_node=40,
                project_root=ROOT,
            )
            source_sha = bundle["arms"][0]["execution_bundle_identity"][
                "python_source_closure_sha256"
            ]
            source_archive = b"fixture repaired source closure\n"
            manifest = _confirmation_manifest_v2(
                bundle,
                source_closure_sha256=source_sha,
                source_archive_sha256=hashlib.sha256(source_archive).hexdigest(),
                run_label="horizon-remote-v2-test",
            )
            run_root = root / "runs" / manifest["run_root_name"]
            run_root.mkdir(parents=True)
            (run_root / "horizon-confirmation-manifest.json").write_text(
                json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            for arm in bundle["arms"]:
                arm_root = run_root / arm["arm_spec"]["arm_id"]
                arm_root.mkdir()
                for name, key in (
                    ("runner-plan.json", "runner_plan"),
                    ("dispatch-plan.json", "dispatch_plan"),
                ):
                    (arm_root / name).write_text(
                        json.dumps(arm[key], sort_keys=True, separators=(",", ":"))
                        + "\n",
                        encoding="utf-8",
                    )
            source_root = root / "source-releases"
            release = source_root / source_sha
            release.mkdir(parents=True)
            (release / "source-closure.tar.gz").write_bytes(source_archive)
            (release / "source-identity.json").write_text(
                json.dumps(
                    {"canonical_bundle": {"sha256": source_sha}},
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            receipt = prepare_remote_attempt_v1(
                run_root=run_root,
                source_release_root=source_root,
                attempt_id="attempt-v2-script-audit",
                attempts_root=root / "attempts",
                site_config=SITE,
            )
            attempt = Path(receipt["attempt_directory"])
            plan = json.loads(
                (attempt / "orchestration-plan.json").read_text(encoding="utf-8")
            )
            reduce_source = (attempt / "scripts/reduce.sh").read_text(
                encoding="utf-8"
            )

        self.assertEqual(list(CONFIRMATION_ARM_IDS), plan["confirmation_contract"]["arm_ids"])
        self.assertEqual(
            len(CONFIRMATION_MASTER_SEEDS),
            plan["confirmation_contract"]["master_seed_count"],
        )
        self.assertEqual(
            "V2_ANALYSIS_OWNS_SINGLE_PASS_REDUCTIONS", plan["reduction"]["mode"]
        )
        self.assertEqual(
            plan["run_root_name"], plan["remote"]["run_release"].rsplit("/", 1)[-1]
        )
        self.assertEqual(
            1, reduce_source.count("o2o_dps.cat2new_fury_horizon_analysis_v2")
        )
        self.assertEqual(
            0, reduce_source.count("-m o2o_dps.fury_multiseed_hpc_reducer_v4")
        )
        self.assertEqual(3, reduce_source.count("--arm-output"))
        self.assertIn("--compact-output", reduce_source)
        self.assertNotIn("p.pop('paired_trace'", reduce_source)


if __name__ == "__main__":
    unittest.main()
