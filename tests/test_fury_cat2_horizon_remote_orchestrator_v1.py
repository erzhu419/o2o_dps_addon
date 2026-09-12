from __future__ import annotations

import json
import hashlib
from pathlib import Path
import tempfile
import threading
import unittest

from o2o_dps.cat2new_fury_horizon_confirmation_v1 import (
    CONFIRMATION_ARM_IDS,
    build_horizon_confirmation_v1,
)
from o2o_dps.fury_cat2_horizon_confirmation_preparation_v1 import (
    _confirmation_manifest,
)
from o2o_dps.fury_cat2_horizon_remote_orchestrator_v1 import (
    FuryCat2HorizonRemoteOrchestratorV1Error,
    _launch_command,
    _marker_probe_command,
    derive_status_v1,
    launch_full_v1,
    launch_smoke_v1,
    prepare_remote_attempt_v1,
    stage_remote_attempt_v1,
    status_remote_attempt_v1,
)
from o2o_dps.hpc_environment_v1 import CommandResult
from tests.test_cat2new_fury_horizon_confirmation_v1 import _source_plan


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "bin/o2obridge.seedfix-v10.withdb.goamd64v1.linux-amd64"
SITE = ROOT / "configs/hpc/site.local.json"


class _FakeTransport:
    def __init__(self) -> None:
        self.run_calls: list[tuple[str, str]] = []
        self.copy_calls: list[tuple[str, str, str]] = []
        self.marker_stdout = "M|prepared|1\n"
        self.fail_runtime_node: str | None = None
        self._lock = threading.Lock()

    def run(self, node: object, command: str, *, timeout: int = 30) -> CommandResult:
        name = str(getattr(node, "name"))
        with self._lock:
            self.run_calls.append((name, command))
        if "printf 'M|%s|" in command:
            return CommandResult(0, self.marker_stdout, "")
        if (
            self.fail_runtime_node == name
            and "BOC_CAT2" not in command
            and "cat2new-runtime" in command
        ):
            return CommandResult(9, "", "runtime missing")
        if "nohup sh" in command:
            return CommandResult(0, f"pid={1000 + int(name[-3:])}\n", "")
        return CommandResult(0, "ok\n", "")

    def copy(
        self,
        node: object,
        local_path: Path,
        remote_path: str,
        *,
        timeout: int = 180,
    ) -> CommandResult:
        with self._lock:
            self.copy_calls.append(
                (str(getattr(node, "name")), local_path.name, remote_path)
            )
        return CommandResult(0, "", "")


class FuryCat2HorizonRemoteOrchestratorV1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        template = cls.root / "template.json"
        template.write_text(
            json.dumps(_source_plan(tuple(range(1, 257)))), encoding="utf-8"
        )
        bundle = build_horizon_confirmation_v1(
            json.loads(template.read_text(encoding="utf-8")),
            bridge_path=BRIDGE,
            bridge_platform="linux-amd64",
            bridge_build_id="seedfix-v10-test",
            workers_per_node=40,
            project_root=ROOT,
        )
        source_sha = bundle["arms"][0]["execution_bundle_identity"][
            "python_source_closure_sha256"
        ]
        source_archive = b"fixture source closure\n"
        manifest = _confirmation_manifest(
            bundle,
            source_closure_sha256=source_sha,
            source_archive_sha256=hashlib.sha256(source_archive).hexdigest(),
            run_label="horizon-remote-test",
        )
        cls.run_root = cls.root / "runs" / manifest["run_root_name"]
        cls.run_root.mkdir(parents=True)
        (cls.run_root / "horizon-confirmation-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n",
            encoding="utf-8",
        )
        for arm in bundle["arms"]:
            arm_id = arm["arm_spec"]["arm_id"]
            arm_root = cls.run_root / arm_id
            arm_root.mkdir()
            for name, key in (
                ("runner-plan.json", "runner_plan"),
                ("dispatch-plan.json", "dispatch_plan"),
            ):
                (arm_root / name).write_text(
                    json.dumps(
                        arm[key], ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    )
                    + "\n",
                    encoding="utf-8",
                )
        cls.source_release_root = cls.root / "source-releases"
        release = cls.source_release_root / source_sha
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
        cls.attempts_root = cls.root / "attempts"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def _prepare(self, attempt_id: str) -> Path:
        receipt = prepare_remote_attempt_v1(
            run_root=self.run_root,
            source_release_root=self.source_release_root,
            attempt_id=attempt_id,
            attempts_root=self.attempts_root,
            site_config=SITE,
        )
        return Path(receipt["attempt_directory"])

    def test_prepare_emits_exact_three_arm_smoke_and_six_node_full_scripts(self) -> None:
        attempt = self._prepare("attempt-script-audit")
        plan = json.loads(
            (attempt / "orchestration-plan.json").read_text(encoding="utf-8")
        )
        self.assertEqual("PREPARED", plan["status"])
        self.assertEqual(3, plan["full"]["arm_count"])
        self.assertEqual(40, plan["full"]["workers_per_arm_per_node"])
        self.assertEqual(120, plan["full"]["aggregate_workers_per_node"])
        self.assertEqual(1, plan["smoke"]["groups_per_arm"])
        self.assertEqual(
            list(CONFIRMATION_ARM_IDS),
            [row["arm_id"] for row in plan["smoke"]["arms"]],
        )

        smoke = (attempt / "scripts/smoke.sh").read_text(encoding="utf-8")
        self.assertEqual(3, smoke.count("--group-id"))
        self.assertNotIn("xargs -r -n1 -P 40", smoke)
        for row in plan["smoke"]["arms"]:
            self.assertIn(f"--group-id {row['group_id']}", smoke)
            self.assertIn(f"state/smoke/arms/{row['arm_id']}", smoke)

        manifest = json.loads(
            (self.run_root / "horizon-confirmation-manifest.json").read_text(
                encoding="utf-8"
            )
        )
        for node in plan["site"]["nodes"]:
            source = (attempt / f"scripts/{node}.sh").read_text(encoding="utf-8")
            self.assertEqual(3, source.count("xargs -r -n1 -P 40"))
            self.assertEqual(3, source.count("o2o_dps.fury_multiseed_hpc_worker_v3"))
            for arm in manifest["arms"]:
                dispatch = json.loads(
                    (self.run_root / arm["dispatch_plan_path"]).read_text(
                        encoding="utf-8"
                    )
                )
                groups = next(
                    row["group_ids"] for row in dispatch["nodes"] if row["name"] == node
                )
                for group_id in groups:
                    self.assertIn(group_id, source)

        reduce_source = (attempt / "scripts/reduce.sh").read_text(encoding="utf-8")
        self.assertEqual(
            3, reduce_source.count("o2o_dps.fury_multiseed_hpc_reducer_v3")
        )
        self.assertEqual(
            1, reduce_source.count("o2o_dps.cat2new_fury_horizon_analysis_v1")
        )
        self.assertIn("compact-analysis.json", reduce_source)
        self.assertIn('"ww_hamstring_cat_timing=$HOME/', reduce_source)

        with self.assertRaisesRegex(
            FuryCat2HorizonRemoteOrchestratorV1Error, "will not be overwritten"
        ):
            prepare_remote_attempt_v1(
                run_root=self.run_root,
                source_release_root=self.source_release_root,
                attempt_id="attempt-script-audit",
                attempts_root=self.attempts_root,
                site_config=SITE,
            )

    def test_marker_probe_stays_below_windows_transport_argv_limit(self) -> None:
        attempt = self._prepare("attempt-compact-marker-probe")
        plan = json.loads(
            (attempt / "orchestration-plan.json").read_text(encoding="utf-8")
        )
        command = _marker_probe_command(plan)

        self.assertLess(len(command.encode("utf-8")), 8192)
        self.assertIn("for node in node001 node002 node003", command)
        self.assertIn("for phase in smoke full", command)
        self.assertEqual(1, command.count("state/full/nodes/$node/running"))
        self.assertNotIn("state/full/nodes/node006/running", command)

    def test_background_launch_uses_ampersand_as_separator(self) -> None:
        attempt = self._prepare("attempt-launch-shell-syntax")
        plan = json.loads(
            (attempt / "orchestration-plan.json").read_text(encoding="utf-8")
        )
        command = _launch_command(plan, "smoke", "smoke.sh", "smoke-launch.log")

        self.assertNotIn("&;", command)
        self.assertIn("< /dev/null & pid=$!;", command)

    def test_stage_uses_existing_transport_and_starts_no_worker(self) -> None:
        attempt = self._prepare("attempt-stage-success")
        transport = _FakeTransport()
        receipt = stage_remote_attempt_v1(
            attempt_directory=attempt,
            source_release_root=self.source_release_root,
            site_config=SITE,
            transport=transport,
        )
        self.assertEqual("PREPARED", receipt["status"])
        self.assertEqual("STAGED", receipt["phase"])
        self.assertEqual(3, len(transport.copy_calls))
        self.assertEqual(
            {"source-closure.tar.gz", "run-bundle.tar.gz", "attempt-bundle.tar.gz"},
            {row[1] for row in transport.copy_calls},
        )
        self.assertFalse(
            any("--group-id" in command for _, command in transport.run_calls)
        )
        self.assertTrue((attempt / "stage-receipt.json").is_file())
        with self.assertRaisesRegex(
            FuryCat2HorizonRemoteOrchestratorV1Error, "already attempted"
        ):
            stage_remote_attempt_v1(
                attempt_directory=attempt,
                source_release_root=self.source_release_root,
                site_config=SITE,
                transport=transport,
            )

    def test_failed_stage_is_preserved_and_cannot_be_retried_in_place(self) -> None:
        attempt = self._prepare("attempt-stage-failed")
        transport = _FakeTransport()
        transport.fail_runtime_node = "node003"
        receipt = stage_remote_attempt_v1(
            attempt_directory=attempt,
            source_release_root=self.source_release_root,
            site_config=SITE,
            transport=transport,
        )
        self.assertEqual("FAILED", receipt["status"])
        status = status_remote_attempt_v1(
            attempt_directory=attempt,
            site_config=SITE,
            transport=transport,
        )
        self.assertEqual("FAILED", status["status"])
        self.assertEqual(["stage"], status["local_failed_actions"])
        with self.assertRaisesRegex(
            FuryCat2HorizonRemoteOrchestratorV1Error, "already attempted"
        ):
            stage_remote_attempt_v1(
                attempt_directory=attempt,
                source_release_root=self.source_release_root,
                site_config=SITE,
                transport=transport,
            )

    def test_smoke_gate_then_launches_all_six_three_arm_supervisors(self) -> None:
        attempt = self._prepare("attempt-launch-gates")
        transport = _FakeTransport()
        stage = stage_remote_attempt_v1(
            attempt_directory=attempt,
            source_release_root=self.source_release_root,
            site_config=SITE,
            transport=transport,
        )
        self.assertEqual("PREPARED", stage["status"])
        smoke = launch_smoke_v1(
            attempt_directory=attempt, site_config=SITE, transport=transport
        )
        self.assertEqual("RUNNING", smoke["status"])
        smoke_launches = [
            row
            for row in transport.run_calls
            if "scripts/smoke.sh" in row[1] and "nohup sh" in row[1]
        ]
        self.assertEqual(1, len(smoke_launches))
        self.assertEqual("node001", smoke_launches[0][0])

        transport.marker_stdout = "\n".join(
            ["M|prepared|1", "M|smoke.complete|1"]
            + [f"M|smoke.arm.{arm}.complete|1" for arm in CONFIRMATION_ARM_IDS]
        ) + "\n"
        full = launch_full_v1(
            attempt_directory=attempt, site_config=SITE, transport=transport
        )
        self.assertEqual("RUNNING", full["status"])
        self.assertEqual(6, len(full["launches"]))
        self.assertTrue(all(row["status"] == "RUNNING" for row in full["launches"]))
        node_launches = [
            row
            for row in transport.run_calls
            if "scripts/node" in row[1] and "nohup sh" in row[1]
        ]
        self.assertEqual(
            [f"node{index:03d}" for index in range(1, 7)],
            sorted(row[0] for row in node_launches),
        )

    def test_status_precedence_preserves_failure_and_requires_complete_reduction(self) -> None:
        plan = {
            "attempt_id": "attempt-status",
            "confirmation_id": "a" * 64,
        }
        counts = {
            "smoke": {arm: 1 for arm in CONFIRMATION_ARM_IDS},
            "full": {arm: 256 for arm in CONFIRMATION_ARM_IDS},
        }
        complete = {
            "prepared": True,
            "smoke.complete": True,
            "reduce.complete": True,
            "analysis.compact": True,
            **{
                f"smoke.arm.{arm}.complete": True for arm in CONFIRMATION_ARM_IDS
            },
            **{
                f"full.node.node{index:03d}.complete": True
                for index in range(1, 7)
            },
            **{
                f"full.arm.{arm}.node{index:03d}.complete": True
                for arm in CONFIRMATION_ARM_IDS
                for index in range(1, 7)
            },
        }
        report = derive_status_v1(plan, complete, counts)
        self.assertEqual("COMPLETE", report["status"])
        failed = dict(complete)
        failed["full.arm.ww_wait_cat_timing.node004.failed"] = True
        report = derive_status_v1(plan, failed, counts)
        self.assertEqual("FAILED", report["status"])
        self.assertIn(
            "full.arm.ww_wait_cat_timing.node004.failed",
            report["failure_markers"],
        )


if __name__ == "__main__":
    unittest.main()
