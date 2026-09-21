from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
import json
from pathlib import Path
import shlex
from tempfile import TemporaryDirectory
import unittest

from o2o_dps.upper_kara_causal_program_remote_contract_v1 import (
    TERMINAL_COMPLETE,
    assign_training_shards_v1,
)
from o2o_dps.upper_kara_causal_program_remote_worker_v1 import (
    freeze_training_winner_v1,
    run_training_shard_v1,
    train_terminal_name_v1,
)
from o2o_dps.upper_kara_causal_program_remote_worker_v2 import (
    _print_receipt_v2,
    _stdout_receipt_v2,
    candidate_manifest_name_v2,
    freeze_training_winner_compact_v2,
    generate_candidate_manifest_v2,
    run_training_shard_compact_v2,
    train_lane_sidecar_name_v2,
)
from scripts.upper_kara_causal_program_remote_submit_v1 import (
    WORKER_MODULE_V2,
    build_upper_kara_causal_program_remote_plan_v2,
    inventory_upper_kara_causal_program_remote_v1,
)
from tests.test_upper_kara_causal_program_remote_v5 import (
    LOADOUT_ID,
    _campaign,
    _composite_program,
    _parent_program,
    _runtime,
    _write_campaign,
)


def _without_completed_at(value: dict) -> dict:
    result = dict(value)
    result.pop("completed_at", None)
    return result


class CompactRemoteWorkerV2Tests(unittest.TestCase):
    def test_candidate_is_generated_once_and_shards_only_replay(self) -> None:
        with TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            parent = _parent_program()
            composite = _composite_program(parent)
            campaign = _campaign(parent)
            campaign_path = _write_campaign(root, campaign)
            candidate_runtime, _ = _runtime(
                parent, composite, accept_composite=True
            )
            calls = 0
            source_generator = candidate_runtime.searched_program_generator

            def counted_generator(**kwargs):
                nonlocal calls
                calls += 1
                return source_generator(**kwargs)

            manifest_path = root / "candidates" / candidate_manifest_name_v2(
                LOADOUT_ID
            )
            generate_candidate_manifest_v2(
                campaign_path=campaign_path,
                loadout_id=LOADOUT_ID,
                output_path=manifest_path,
                bridge_path=root,
                bridge_cwd=root,
                runtime_binding_path=root,
                offline_guide_artifact_path=root,
                runtime=replace(
                    candidate_runtime,
                    searched_program_generator=counted_generator,
                ),
            )
            self.assertEqual(1, calls)

            replay_runtime, _ = _runtime(
                parent, composite, accept_composite=True
            )

            def forbidden_generator(**_):
                raise AssertionError("a training shard regenerated candidates")

            replay_only = replace(
                replay_runtime,
                searched_program_generator=forbidden_generator,
            )
            training_root = root / "train"
            for shard in assign_training_shards_v1(campaign):
                terminal = run_training_shard_compact_v2(
                    campaign_path=campaign_path,
                    candidate_manifest_path=manifest_path,
                    loadout_id=shard.loadout_id,
                    seed_shard_index=shard.seed_shard_index,
                    lane_sidecar_path=(
                        training_root / train_lane_sidecar_name_v2(shard)
                    ),
                    output_path=training_root / train_terminal_name_v1(shard),
                    replay_workers=4,
                    runtime=replay_only,
                )
                self.assertEqual(TERMINAL_COMPLETE, terminal["terminal_status"])
                self.assertFalse(
                    terminal["training_contract"][
                        "candidate_manifest_generated_in_training_shard"
                    ]
                )
                self.assertNotIn("programs", terminal)
                self.assertNotIn("lanes", terminal)
            self.assertEqual(1, calls)

    def test_compact_freeze_is_semantically_identical_to_v1(self) -> None:
        with TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            parent = _parent_program()
            composite = _composite_program(parent)
            campaign = _campaign(parent)
            campaign_path = _write_campaign(root, campaign)

            v1_root = root / "v1"
            for shard in assign_training_shards_v1(campaign):
                runtime, _ = _runtime(
                    parent, composite, accept_composite=True
                )
                run_training_shard_v1(
                    campaign_path=campaign_path,
                    loadout_id=shard.loadout_id,
                    seed_shard_index=shard.seed_shard_index,
                    output_path=v1_root / train_terminal_name_v1(shard),
                    replay_workers=4,
                    runtime=runtime,
                )
            v1_frozen = freeze_training_winner_v1(
                campaign_path=campaign_path,
                training_root=v1_root,
                output_path=root / "v1-frozen.json",
            )

            candidate_root = root / "candidates"
            manifest_path = candidate_root / candidate_manifest_name_v2(
                LOADOUT_ID
            )
            candidate_runtime, _ = _runtime(
                parent, composite, accept_composite=True
            )
            generate_candidate_manifest_v2(
                campaign_path=campaign_path,
                loadout_id=LOADOUT_ID,
                output_path=manifest_path,
                bridge_path=root,
                bridge_cwd=root,
                runtime_binding_path=root,
                offline_guide_artifact_path=root,
                runtime=candidate_runtime,
            )
            v2_root = root / "v2"
            for shard in assign_training_shards_v1(campaign):
                runtime, _ = _runtime(
                    parent, composite, accept_composite=True
                )
                run_training_shard_compact_v2(
                    campaign_path=campaign_path,
                    candidate_manifest_path=manifest_path,
                    loadout_id=shard.loadout_id,
                    seed_shard_index=shard.seed_shard_index,
                    lane_sidecar_path=v2_root / train_lane_sidecar_name_v2(shard),
                    output_path=v2_root / train_terminal_name_v1(shard),
                    replay_workers=4,
                    runtime=runtime,
                )
            v2_frozen = freeze_training_winner_compact_v2(
                campaign_path=campaign_path,
                candidate_root=candidate_root,
                training_root=v2_root,
                output_path=root / "v2-frozen.json",
            )

        self.assertEqual(
            _without_completed_at(v1_frozen),
            _without_completed_at(v2_frozen),
        )

    def test_missing_manifest_or_sidecar_fails_closed(self) -> None:
        with TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            parent = _parent_program()
            composite = _composite_program(parent)
            campaign = _campaign(parent)
            campaign_path = _write_campaign(root, campaign)
            runtime, _ = _runtime(parent, composite, accept_composite=True)
            shard = assign_training_shards_v1(campaign)[0]
            with self.assertRaises(FileNotFoundError):
                run_training_shard_compact_v2(
                    campaign_path=campaign_path,
                    candidate_manifest_path=root / "missing.json",
                    loadout_id=shard.loadout_id,
                    seed_shard_index=shard.seed_shard_index,
                    lane_sidecar_path=root / "missing-sidecar.json",
                    output_path=root / "missing-terminal.json",
                    runtime=runtime,
                )

            candidate_root = root / "candidates"
            manifest_path = candidate_root / candidate_manifest_name_v2(
                LOADOUT_ID
            )
            generate_candidate_manifest_v2(
                campaign_path=campaign_path,
                loadout_id=LOADOUT_ID,
                output_path=manifest_path,
                bridge_path=root,
                bridge_cwd=root,
                runtime_binding_path=root,
                offline_guide_artifact_path=root,
                runtime=runtime,
            )
            with self.assertRaises(FileNotFoundError):
                freeze_training_winner_compact_v2(
                    campaign_path=campaign_path,
                    candidate_root=candidate_root,
                    training_root=root / "empty-train",
                    output_path=root / "never.json",
                )
            self.assertFalse((root / "never.json").exists())

    def test_stdout_receipt_is_under_one_kib_and_has_no_payload_arrays(self) -> None:
        receipt = _stdout_receipt_v2(
            phase="train",
            output_path="/remote/train/seed-000.json",
            payload={
                "terminal_status": "COMPLETE",
                "program_count": 387,
                "lane_count": 387,
                "programs": [{"large": "not printed"}] * 100,
                "lanes": [{"large": "not printed"}] * 100,
            },
        )
        stream = StringIO()
        with redirect_stdout(stream):
            _print_receipt_v2(receipt)
        wire = stream.getvalue().strip()
        self.assertLess(len(wire.encode("utf-8")), 1_024)
        self.assertNotIn('"programs"', wire)
        self.assertNotIn('"lanes"', wire)
        self.assertEqual("COMPLETE", json.loads(wire)["status"])


class CompactRemotePlanV2Tests(unittest.TestCase):
    def test_256_shard_plan_has_one_candidate_task_and_no_regeneration(self) -> None:
        campaign = (
            Path(__file__).resolve().parents[1]
            / "configs/evaluation/upper_kara_fixed_burst_block_remote_v5_256x256_v1.json"
        )
        common = {
            "shared_home": "/home/tester",
            "run_id": "compact-v2-plan",
            "campaign": campaign,
            "bridge_name": "bridge-v23.linux-amd64",
        }
        candidate = build_upper_kara_causal_program_remote_plan_v2(
            phase="candidate", **common
        )
        train = build_upper_kara_causal_program_remote_plan_v2(
            phase="train", **common
        )
        freeze = build_upper_kara_causal_program_remote_plan_v2(
            phase="freeze", **common
        )

        self.assertEqual(1, candidate["task_count"])
        self.assertEqual(256, train["task_count"])
        self.assertEqual(256, len(train["terminal_outputs"]))
        self.assertEqual(512, len(train["artifact_outputs"]))
        self.assertTrue(
            train["phase_contract"]["candidate_generation_once_per_loadout"]
        )
        self.assertFalse(
            train["phase_contract"]["training_shards_may_generate_candidates"]
        )
        for spec in train["task_specs"]:
            self.assertIn(WORKER_MODULE_V2, spec["cmd"])
            self.assertIn("--candidate-manifest", spec["cmd"])
            self.assertIn("--lane-sidecar", spec["cmd"])
            self.assertNotIn("--offline-guide-artifact", spec["cmd"])
        self.assertEqual(
            ["candidate", "train", "freeze", "eval", "summarize"],
            freeze["phase_contract"]["order"],
        )
        self.assertEqual(
            256,
            sum(
                row["role"] == "TRAIN_COMMIT_V2"
                for row in freeze["semantic_prerequisites"]
            ),
        )
        self.assertEqual(
            1,
            sum(
                row["role"] == "CANDIDATE_MANIFEST_V2"
                for row in freeze["semantic_prerequisites"]
            ),
        )

    def test_train_inventory_blocks_until_candidate_manifest_exists(self) -> None:
        class Lock:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        class Scheduler:
            def __init__(
                self,
                existing: set[str],
                projections: dict[str, dict] | None = None,
            ) -> None:
                self.existing = existing
                self.projections = projections or {}

            def state_lock(self, **_):
                return Lock()

            @staticmethod
            def load_state():
                return {"tasks": []}

            def run_on(self, _node, command, **_):
                parts = shlex.split(command)
                if "upper_kara_required_path_batch_v1" in parts[2]:
                    probes = json.loads(parts[3])
                    missing = [
                        {"index": row["index"], "path": row["path"]}
                        for row in probes
                        if row["path"] not in self.existing
                    ]
                    return 0, json.dumps(missing), ""
                if "os.path.exists" in parts[2]:
                    return 0, "[]", ""
                if "upper_kara_semantic_projection_batch_v1" in parts[2]:
                    return (
                        0,
                        json.dumps(
                            [
                                {
                                    "path": path,
                                    "projection": self.projections[path],
                                }
                                for path in parts[3:]
                            ]
                        ),
                        "",
                    )
                raise AssertionError(f"unexpected remote command: {command}")

        campaign = (
            Path(__file__).resolve().parents[1]
            / "configs/evaluation/upper_kara_fixed_burst_block_remote_v5_256x256_v1.json"
        )
        plan = build_upper_kara_causal_program_remote_plan_v2(
            shared_home="/home/tester",
            run_id="compact-v2-order-gate",
            phase="train",
            campaign=campaign,
            bridge_name="bridge-v23.linux-amd64",
        )
        candidate_path = next(
            row["path"]
            for row in plan["required_remote_paths"]
            if "/results/candidates/" in row["path"]
        )
        existing = {row["path"] for row in plan["required_remote_paths"]}
        inventory = inventory_upper_kara_causal_program_remote_v1(
            Scheduler(existing - {candidate_path}), plan
        )
        self.assertEqual("NOT_READY_TO_QUEUE", inventory["status"])
        self.assertEqual(
            [candidate_path],
            [row["path"] for row in inventory["missing_required_paths"]],
        )
        candidate_projection = {
            "schema": plan["semantic_prerequisites"][0]["schema"],
            "campaign_id": plan["campaign_id"],
            "build_id": plan["build_id"],
            "campaign_contract": plan["campaign_contract"],
            "loadout_id": plan["semantic_prerequisites"][0][
                "expected_fields"
            ]["loadout_id"],
        }
        ready = inventory_upper_kara_causal_program_remote_v1(
            Scheduler(existing, {candidate_path: candidate_projection}), plan
        )
        self.assertEqual("READY_TO_QUEUE", ready["status"])
        self.assertEqual(1, ready["semantic_terminal_projection_count"])


if __name__ == "__main__":
    unittest.main()
