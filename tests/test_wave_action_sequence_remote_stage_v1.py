from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from o2o_dps.wave_action_sequence_remote_contract_v1 import (
    UPPER_KARA_MANIFEST_SCHEMA,
)
from scripts.wave_action_sequence_remote_stage_v1 import (
    DEFAULT_COMPACT_INPUTS,
    DEVELOPMENT_CASE_BUILDER,
    MANIFEST_SCHEMA,
    NODE_NAMES,
    OFFLINE_GUIDE_ARTIFACT,
    UPPER_KARA_CASE_BUILDER,
    UPPER_KARA_COMPACT_INPUTS,
    assign_exact_cell_shards_v1,
    build_wave_action_sequence_stage_plan_v1,
    load_exact_cell_manifest_v1,
    stage_wave_action_sequence_v1,
)


def _cells(
    count: int = 12,
    *,
    case_builder: str | None = None,
) -> list[dict]:
    cells = []
    ranks = (7, 11, 9)
    strata = ("q05", "q60", "q95", "multi_2")
    for index in range(count):
        row = {
            "cell_id": f"upper-kara-wave-{index:02d}-build-{index:02d}",
            "search_cell": {
                "scenario_id": "upper-kara",
                "wave_or_boss_id": f"wave-{index:02d}",
                "exact_build_id": f"build-{index:02d}",
                "talents": [{"talent": "tree_1", "rank": index}],
                "equipment": [
                    {"slot": "main_hand", "item_id": 10_000 + index}
                ],
                "derived_mechanics": {"target_count": 1 + index % 4},
                "environment_branch_id": "measured-team-clock",
            },
            "seeds": list(range(20_000 + index * 100, 20_064 + index * 100)),
        }
        if case_builder is not None:
            row["case_builder"] = case_builder
            row["case_params"] = {
                "representative_rank": ranks[(index // 4) % len(ranks)],
                "stratum": strata[index % len(strata)],
                "attackability_branch": "full_wave",
            }
        cells.append(row)
    return cells


def _write_manifest(directory: str, cells: list[dict] | None = None) -> Path:
    path = Path(directory) / "exact-cells.json"
    path.write_text(
        json.dumps({"schema": MANIFEST_SCHEMA, "cells": cells or _cells()}),
        encoding="utf-8",
    )
    return path


def _write_upper_kara_builder_manifest(directory: str) -> Path:
    remote_cells = _cells(case_builder=UPPER_KARA_CASE_BUILDER)
    cells = [
        {
            "representative_rank": row["case_params"]["representative_rank"],
            "stratum": row["case_params"]["stratum"],
            "search_cell": row["search_cell"],
        }
        for row in remote_cells
    ]
    path = Path(directory) / "upper-kara-builder-cells.json"
    path.write_text(
        json.dumps({
            "schema": UPPER_KARA_MANIFEST_SCHEMA,
            "seeds": remote_cells[0]["seeds"],
            "cells": cells,
        }),
        encoding="utf-8",
    )
    return path


class _FakeScheduler:
    def __init__(self) -> None:
        self.commands: list[tuple[str, str]] = []

    def run_on(self, node, command, **_kwargs):
        self.commands.append((node, command))
        if command.startswith("cd; pwd; stat"):
            return 0, "/home/tester\n42:100\n", ""
        return 0, "", ""

    @staticmethod
    def _ssh_rsync_shell_for_node(node):
        return f"ssh-{node}"

    @staticmethod
    def _ssh_target_for_node(node):
        return f"target-{node}"


class WaveActionSequenceRemoteStageV1Tests(unittest.TestCase):
    def test_manifest_assignment_is_six_nonempty_disjoint_complete_shards(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = load_exact_cell_manifest_v1(_write_manifest(directory))
        shards = assign_exact_cell_shards_v1(manifest)
        self.assertEqual(6, len(shards))
        self.assertEqual([2] * 6, [len(shard) for shard in shards])
        sets = [{row["cell_id"] for row in shard} for shard in shards]
        for left in range(6):
            for right in range(left + 1, 6):
                self.assertFalse(sets[left] & sets[right])
        self.assertEqual(
            {row["cell_id"] for row in manifest["cells"]},
            set().union(*sets),
        )

    def test_manifest_rejects_duplicate_exact_identity_and_short_seed_panel(self):
        duplicate = _cells(6)
        duplicate[-1]["search_cell"] = duplicate[0]["search_cell"]
        short = _cells(6)
        short[0]["seeds"] = short[0]["seeds"][:63]
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "duplicate exact"):
                load_exact_cell_manifest_v1(
                    _write_manifest(directory, duplicate)
                )
            with self.assertRaisesRegex(ValueError, "at least 64"):
                load_exact_cell_manifest_v1(
                    _write_manifest(directory, short)
                )

    def test_optional_evaluation_seed_panel_is_disjoint_and_preserved(self):
        cells = _cells(6)
        for index, row in enumerate(cells):
            row["evaluation_seeds"] = list(
                range(90_000 + index * 100, 90_064 + index * 100)
            )
        with tempfile.TemporaryDirectory() as directory:
            manifest = load_exact_cell_manifest_v1(
                _write_manifest(directory, cells)
            )
        self.assertEqual(cells[0]["evaluation_seeds"], manifest["cells"][0]["evaluation_seeds"])

        cells[0]["evaluation_seeds"][0] = cells[0]["seeds"][0]
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "must be disjoint"):
                load_exact_cell_manifest_v1(_write_manifest(directory, cells))

    def test_upper_kara_adapter_propagates_disjoint_evaluation_panel(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _write_upper_kara_builder_manifest(directory)
            document = json.loads(path.read_text(encoding="utf-8"))
            document["evaluation_seeds"] = list(range(91_000, 91_064))
            path.write_text(json.dumps(document), encoding="utf-8")
            manifest = load_exact_cell_manifest_v1(path)
        self.assertTrue(all(
            row["evaluation_seeds"] == document["evaluation_seeds"]
            for row in manifest["cells"]
        ))

    def test_stage_plan_is_local_only_compact_and_bridge_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = Path(directory) / "o2obridge.seedfix-v21.withdb.goamd64v1.linux-amd64"
            bridge.write_bytes(b"ELF")
            manifest = _write_manifest(directory)
            plan = build_wave_action_sequence_stage_plan_v1(
                shared_home="/home/tester",
                run_id="finite-search-001",
                bridge=bridge,
                cell_manifest=manifest,
                compact_inputs=(),
            )
        self.assertEqual("DRY_RUN_NO_REMOTE_IO", plan["status"])
        self.assertEqual(12, plan["cell_count"])
        self.assertEqual([DEVELOPMENT_CASE_BUILDER], plan["case_builders"])
        self.assertEqual([2] * 6, plan["shard_cell_counts"])
        self.assertTrue(plan["bridge"].endswith(
            "/bin/o2obridge.seedfix-v21.withdb.goamd64v1.linux-amd64"
        ))
        self.assertEqual(8, len(plan["copy_specs"]))
        self.assertEqual(
            str(OFFLINE_GUIDE_ARTIFACT.resolve()),
            plan["compact_inputs"][0],
        )
        self.assertFalse(plan["raw_offline_data_staged"])
        self.assertFalse(plan["checkpoint_staged"])
        self.assertFalse(any(
            row["source"].lower().endswith((".csv", ".tsv"))
            for row in plan["copy_specs"]
        ))

    def test_default_compact_closure_contains_only_model_or_derived_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = Path(directory) / "o2obridge.seedfix-v21.withdb.goamd64v1.linux-amd64"
            bridge.write_bytes(b"ELF")
            manifest = _write_manifest(directory)
            plan = build_wave_action_sequence_stage_plan_v1(
                shared_home="/home/tester",
                run_id="finite-search-002",
                bridge=bridge,
                cell_manifest=manifest,
            )
        compact_sources = {
            str(path.resolve()) for path in DEFAULT_COMPACT_INPUTS
        }
        staged_sources = {row["source"] for row in plan["copy_specs"]}
        self.assertTrue(compact_sources <= staged_sources)
        self.assertFalse(any(
            source.lower().endswith((".csv", ".tsv"))
            for source in compact_sources
        ))

    def test_upper_kara_manifest_forces_compact_runtime_closure(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = Path(directory) / "o2obridge.seedfix-v21.withdb.goamd64v1.linux-amd64"
            bridge.write_bytes(b"ELF")
            manifest = _write_upper_kara_builder_manifest(directory)
            plan = build_wave_action_sequence_stage_plan_v1(
                shared_home="/home/tester",
                run_id="upper-kara-search-001",
                bridge=bridge,
                cell_manifest=manifest,
                compact_inputs=(),
            )
        expected = {
            str(OFFLINE_GUIDE_ARTIFACT.resolve()),
            *(str(path.resolve()) for path in UPPER_KARA_COMPACT_INPUTS),
        }
        self.assertEqual(expected, set(plan["compact_inputs"]))
        self.assertEqual(
            [UPPER_KARA_CASE_BUILDER], plan["case_builders"]
        )
        self.assertIsNotNone(plan["upper_kara_compact_closure"])
        staged_sources = {row["source"] for row in plan["copy_specs"]}
        self.assertTrue(expected <= staged_sources)
        self.assertFalse(any(
            source.lower().endswith((".csv", ".tsv", ".parquet", ".ckpt"))
            for source in staged_sources
        ))

    def test_execute_stages_one_shared_copy_and_never_launches(self):
        scheduler = _FakeScheduler()
        with tempfile.TemporaryDirectory() as directory:
            bridge = Path(directory) / "o2obridge.seedfix-v21.withdb.goamd64v1.linux-amd64"
            bridge.write_bytes(b"ELF")
            manifest = _write_manifest(directory)
            with (
                patch(
                    "scripts.wave_action_sequence_remote_stage_v1._scheduler",
                    return_value=scheduler,
                ),
                patch(
                    "scripts.wave_action_sequence_remote_stage_v1.subprocess.run"
                ) as run,
            ):
                receipt = stage_wave_action_sequence_v1(
                    staging_node=NODE_NAMES[0],
                    run_id="finite-search-003",
                    bridge=bridge,
                    cell_manifest=manifest,
                    compact_inputs=(),
                )
        self.assertEqual("STAGED_NO_JOB_LAUNCHED", receipt["status"])
        self.assertEqual(8, run.call_count)
        contra_call = next(
            call
            for call in run.call_args_list
            if "Contra_new" in " ".join(str(row) for row in call.args[0])
        )
        contra_command = contra_call.args[0]
        self.assertIn("--include=*.lua", contra_command)
        self.assertIn("--include=*.toc", contra_command)
        self.assertIn("--include=*.xml", contra_command)
        self.assertEqual(set(NODE_NAMES), set(receipt["shared_home_receipts"]))
        self.assertFalse(any(
            " submit " in command or " dispatch " in command
            for _, command in scheduler.commands
        ))


if __name__ == "__main__":
    unittest.main()
