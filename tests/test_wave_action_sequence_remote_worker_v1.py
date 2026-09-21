from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from o2o_dps.causal_guard_v1 import ObservableCausalGuardV1
from o2o_dps.development_wave_case_v1 import build_development_wave_case_v1
from o2o_dps.upper_kara_exact_cell_case_v1 import (
    build_upper_kara_exact_cell_case_v1,
    search_cell_from_upper_kara_case_v1,
)
from o2o_dps.wave_action_sequence_pilot_v1 import search_cell_from_case_v1
from o2o_dps.wave_action_sequence_remote_contract_v1 import (
    MANIFEST_SCHEMA,
    UPPER_KARA_CASE_BUILDER,
    UPPER_KARA_MANIFEST_SCHEMA,
    adapt_upper_kara_exact_cell_manifest_v1,
    load_exact_cell_manifest_v1,
)
from o2o_dps.wave_action_sequence_remote_worker_v1 import (
    CELL_TERMINAL_SCHEMA,
    SHARD_SUMMARY_SCHEMA,
    SUPPORTED_CASE_BUILDER,
    _build_upper_kara_case_cached_v1,
    _default_cell_runner,
    plan_wave_action_sequence_shard_v1,
    run_wave_action_sequence_shard_v1,
)


def _manifest(directory: str, *, first_builder: str = SUPPORTED_CASE_BUILDER) -> Path:
    first_seed = 2026091417
    exact = search_cell_from_case_v1(
        build_development_wave_case_v1(first_seed)
    ).to_dict()
    cells = []
    for index in range(6):
        identity = deepcopy(exact)
        if index:
            identity["wave_or_boss_id"] = f"not-built-wave-{index}"
        cells.append(
            {
                "cell_id": f"cell-{index}",
                "case_builder": (
                    first_builder if index == 0 else "upper_kara_exact_cell_v1"
                ),
                "search_cell": identity,
                "seeds": list(range(first_seed + index * 100, first_seed + index * 100 + 64)),
            }
        )
    path = Path(directory) / "cells.json"
    path.write_text(
        json.dumps({"schema": MANIFEST_SCHEMA, "cells": cells}),
        encoding="utf-8",
    )
    return path


def _fake_pilot(_cell, config):
    return {
        "schema": "wave_action_sequence_native_pilot/v1",
        "search": {
            "status": "INCOMPLETE_DEPTH_BUDGET",
            "completed_depth": 2,
            "cell": deepcopy(_cell["search_cell"]),
        },
        "received_replay_workers": config["replay_workers"],
        "received_continuation_max_steps": config["continuation_max_steps"],
    }


def _upper_eval_manifest(directory: str) -> tuple[Path, object]:
    first_seed = 2026091417
    base = build_upper_kara_exact_cell_case_v1(
        first_seed,
        representative_rank=7,
        stratum="q05",
    )
    exact = search_cell_from_upper_kara_case_v1(base).to_dict()
    cells = []
    for index in range(6):
        identity = deepcopy(exact)
        if index:
            identity["wave_or_boss_id"] = f"unsupported-eval-wave-{index}"
            identity["exact_build_id"] = f"unsupported-eval-build-{index}"
        row = {
            "cell_id": f"cell-{index}",
            "case_builder": (
                UPPER_KARA_CASE_BUILDER if index == 0 else "unsupported_builder"
            ),
            "case_params": (
                {
                    "representative_rank": 7,
                    "stratum": "q05",
                    "attackability_branch": "full_wave",
                    "precombat_self_actions": None,
                    "pull_time_ms": 3_000,
                    "player_consumes": None,
                }
                if index == 0
                else {}
            ),
            "search_cell": identity,
            "seeds": list(range(first_seed, first_seed + 64)),
        }
        if index == 0:
            row["evaluation_seeds"] = list(
                range(first_seed + 1_000, first_seed + 1_064)
            )
        cells.append(row)
    path = Path(directory) / "upper-eval-cells.json"
    path.write_text(
        json.dumps({"schema": MANIFEST_SCHEMA, "cells": cells}),
        encoding="utf-8",
    )
    return path, base


class WaveActionSequenceRemoteWorkerV1Tests(unittest.TestCase):
    def _paths(self, directory: str) -> tuple[Path, Path, Path]:
        root = Path(directory)
        bridge = root / "o2obridge.v20.linux-amd64"
        bridge.write_bytes(b"ELF")
        bridge_cwd = root / "wowsims"
        bridge_cwd.mkdir()
        return bridge, bridge_cwd, root / "cells-out"

    def test_development_exact_cell_dry_run_rebuilds_identity_without_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = _manifest(directory)
            bridge, bridge_cwd, output = self._paths(directory)
            plan = plan_wave_action_sequence_shard_v1(
                cell_manifest=manifest,
                shard_index=0,
                bridge=bridge,
                bridge_cwd=bridge_cwd,
            )
            self.assertFalse(output.exists())
        self.assertEqual("READY_DEVELOPMENT_ONLY", plan["status"])
        self.assertEqual("READY_DEVELOPMENT_EXACT_CELL", plan["cells"][0]["status"])
        self.assertFalse(plan["claim_boundary"]["bridge_executed"])
        self.assertFalse(plan["claim_boundary"]["arbitrary_manifest_exact_cells_supported"])

    def test_cli_development_dry_run_is_end_to_end_and_side_effect_free(self):
        explicit_guard = ObservableCausalGuardV1(
            mh_swing_remaining_lte_ms=450
        )
        with tempfile.TemporaryDirectory() as directory:
            manifest = _manifest(directory)
            bridge, bridge_cwd, output = self._paths(directory)
            summary = Path(directory) / "summary.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "o2o_dps.wave_action_sequence_remote_worker_v1",
                    "--cell-manifest",
                    str(manifest),
                    "--shard-index",
                    "0",
                    "--bridge",
                    str(bridge),
                    "--bridge-cwd",
                    str(bridge_cwd),
                    "--output-dir",
                    str(output),
                    "--summary",
                    str(summary),
                    "--guard-option-json",
                    json.dumps(explicit_guard.to_dict()),
                    "--dry-run",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            payload = json.loads(completed.stdout)
            self.assertFalse(output.exists())
            self.assertFalse(summary.exists())
        self.assertEqual("READY_DEVELOPMENT_ONLY", payload["status"])
        self.assertEqual(
            [explicit_guard.to_dict()], payload["worker_config"]["guard_options"]
        )
        self.assertEqual(
            "EXPLICIT_CLI_JSON",
            payload["worker_config"]["guard_option_source"],
        )

    def test_fake_runner_writes_terminal_and_summary_and_preserves_inner_status(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = _manifest(directory)
            bridge, bridge_cwd, output = self._paths(directory)
            summary = Path(directory) / "summary.json"
            result = run_wave_action_sequence_shard_v1(
                cell_manifest=manifest,
                shard_index=0,
                bridge=bridge,
                bridge_cwd=bridge_cwd,
                output_dir=output,
                summary=summary,
                cell_runner=_fake_pilot,
            )
            terminal_path = output / "cell-0.json"
            terminal = json.loads(terminal_path.read_text(encoding="utf-8"))
            persisted_summary = json.loads(summary.read_text(encoding="utf-8"))
            leftovers = list(Path(directory).rglob("*.tmp"))
        self.assertEqual("DONE", result["status"])
        self.assertEqual(CELL_TERMINAL_SCHEMA, terminal["schema"])
        self.assertEqual("DONE", terminal["status"])
        self.assertEqual(
            "INCOMPLETE_DEPTH_BUDGET", terminal["pilot"]["search"]["status"]
        )
        self.assertEqual(64, terminal["pilot"]["received_replay_workers"])
        self.assertEqual(32, terminal["pilot"]["received_continuation_max_steps"])
        self.assertEqual(2, len(terminal["resolved_guard_options"]))
        self.assertEqual(SHARD_SUMMARY_SCHEMA, persisted_summary["schema"])
        self.assertEqual([], leftovers)

    def test_retry_skips_existing_done_terminal_without_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = _manifest(directory)
            bridge, bridge_cwd, output = self._paths(directory)
            first_summary = Path(directory) / "summary-1.json"
            second_summary = Path(directory) / "summary-2.json"
            run_wave_action_sequence_shard_v1(
                cell_manifest=manifest,
                shard_index=0,
                bridge=bridge,
                bridge_cwd=bridge_cwd,
                output_dir=output,
                summary=first_summary,
                cell_runner=_fake_pilot,
            )
            terminal = output / "cell-0.json"
            before = terminal.read_bytes()

            def must_not_run(_cell, _config):
                raise AssertionError("completed cell was re-run")

            result = run_wave_action_sequence_shard_v1(
                cell_manifest=manifest,
                shard_index=0,
                bridge=bridge,
                bridge_cwd=bridge_cwd,
                output_dir=output,
                summary=second_summary,
                cell_runner=must_not_run,
            )
            after = terminal.read_bytes()
        self.assertEqual(before, after)
        self.assertEqual("DONE", result["status"])
        self.assertEqual("SKIPPED_EXISTING_DONE", result["cells"][0]["status"])

    def test_runner_failure_reason_is_preserved_and_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = _manifest(directory)
            bridge, bridge_cwd, output = self._paths(directory)

            def fail(_cell, _config):
                raise RuntimeError("deliberate bridge failure")

            first = run_wave_action_sequence_shard_v1(
                cell_manifest=manifest,
                shard_index=0,
                bridge=bridge,
                bridge_cwd=bridge_cwd,
                output_dir=output,
                summary=Path(directory) / "summary-1.json",
                cell_runner=fail,
            )
            terminal_path = output / "cell-0.json"
            before = terminal_path.read_bytes()
            terminal = json.loads(before)
            second = run_wave_action_sequence_shard_v1(
                cell_manifest=manifest,
                shard_index=0,
                bridge=bridge,
                bridge_cwd=bridge_cwd,
                output_dir=output,
                summary=Path(directory) / "summary-2.json",
                cell_runner=_fake_pilot,
            )
            after = terminal_path.read_bytes()
        self.assertEqual("FAILED", first["status"])
        self.assertEqual("deliberate bridge failure", terminal["reason"])
        self.assertEqual("RuntimeError", terminal["error_type"])
        self.assertEqual(before, after)
        self.assertEqual("FAILED", second["status"])
        self.assertEqual("SKIPPED_EXISTING_FAILED", second["cells"][0]["status"])

    def test_unsupported_builder_fails_closed_without_calling_runner(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = _manifest(directory, first_builder="upper_kara_exact_cell_v1")
            bridge, bridge_cwd, output = self._paths(directory)

            def must_not_run(_cell, _config):
                raise AssertionError("unsupported case reached runner")

            result = run_wave_action_sequence_shard_v1(
                cell_manifest=manifest,
                shard_index=0,
                bridge=bridge,
                bridge_cwd=bridge_cwd,
                output_dir=output,
                summary=Path(directory) / "summary.json",
                cell_runner=must_not_run,
            )
            terminal = json.loads(
                (output / "cell-0.json").read_text(encoding="utf-8")
            )
        self.assertEqual("FAILED", result["status"])
        self.assertEqual("FAILED_UNSUPPORTED_CASE_BUILDER", terminal["status"])
        self.assertIn("not reconstructible", terminal["reason"])

    def test_existing_summary_is_reused_without_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = _manifest(directory)
            bridge, bridge_cwd, output = self._paths(directory)
            summary = Path(directory) / "summary.json"
            run_wave_action_sequence_shard_v1(
                cell_manifest=manifest,
                shard_index=0,
                bridge=bridge,
                bridge_cwd=bridge_cwd,
                output_dir=output,
                summary=summary,
                cell_runner=_fake_pilot,
            )
            before = summary.read_bytes()
            reused = run_wave_action_sequence_shard_v1(
                cell_manifest=manifest,
                shard_index=0,
                bridge=bridge,
                bridge_cwd=bridge_cwd,
                output_dir=output,
                summary=summary,
                cell_runner=lambda *_: (_ for _ in ()).throw(AssertionError()),
            )
            after = summary.read_bytes()
        self.assertEqual(before, after)
        self.assertTrue(reused["reused_existing_summary"])

    def test_continuation_requires_a_guide(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = _manifest(directory)
            bridge, bridge_cwd, _ = self._paths(directory)
            with self.assertRaisesRegex(ValueError, "requires at least one"):
                plan_wave_action_sequence_shard_v1(
                    cell_manifest=manifest,
                    shard_index=0,
                    bridge=bridge,
                    bridge_cwd=bridge_cwd,
                    expert_guides=(),
                    continuation_max_steps=32,
                )

    def test_upper_kara_source_manifest_requires_explicit_seed_panel(self):
        first_seed = 2026091417
        exact = search_cell_from_case_v1(
            build_development_wave_case_v1(first_seed)
        ).to_dict()
        rows = []
        ranks = (7, 11, 9)
        strata = ("q05", "q60", "q95", "multi_2")
        pairs = [(rank, stratum) for rank in ranks for stratum in strata]
        for index, (rank, stratum) in enumerate(pairs):
            identity = deepcopy(exact)
            identity["wave_or_boss_id"] = f"upper-kara-wave-{index}"
            identity["exact_build_id"] = f"historical-build-{rank}"
            rows.append(
                {
                    "representative_rank": rank,
                    "stratum": stratum,
                    "search_cell": identity,
                }
            )
        source = {
            "schema": UPPER_KARA_MANIFEST_SCHEMA,
            "precombat": None,
            "cells": rows,
        }
        with self.assertRaisesRegex(ValueError, "requires at least 64"):
            adapt_upper_kara_exact_cell_manifest_v1(source)
        source["seeds"] = list(range(first_seed, first_seed + 64))
        adapted = adapt_upper_kara_exact_cell_manifest_v1(source)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "upper.json"
            path.write_text(json.dumps(source), encoding="utf-8")
            loaded = load_exact_cell_manifest_v1(path)
        self.assertEqual(MANIFEST_SCHEMA, adapted["schema"])
        self.assertEqual(
            "SMOKE_MANIFEST_ONLY_PREPARED_NOT_RUN", adapted["status"]
        )
        self.assertFalse(adapted["execution_evidence"])
        self.assertEqual(12, len(loaded["cells"]))
        self.assertTrue(all(
            row["case_builder"] == UPPER_KARA_CASE_BUILDER
            for row in loaded["cells"]
        ))
        self.assertTrue(all(len(row["seeds"]) == 64 for row in loaded["cells"]))

    def test_upper_kara_allowlisted_builder_rebuilds_every_seed(self):
        first_seed = 2026091417
        base = build_upper_kara_exact_cell_case_v1(
            first_seed,
            representative_rank=7,
            stratum="q05",
        )
        exact = search_cell_from_upper_kara_case_v1(base).to_dict()
        other = search_cell_from_case_v1(
            build_development_wave_case_v1(first_seed)
        ).to_dict()
        cells = [
            {
                "cell_id": "cell-0",
                "case_builder": UPPER_KARA_CASE_BUILDER,
                "case_params": {
                    "representative_rank": 7,
                    "stratum": "q05",
                    "attackability_branch": "full_wave",
                    "precombat_self_actions": None,
                    "pull_time_ms": 3000,
                    "player_consumes": None,
                },
                "search_cell": exact,
                "seeds": list(range(first_seed, first_seed + 64)),
            }
        ]
        for index in range(1, 6):
            identity = deepcopy(other)
            identity["wave_or_boss_id"] = f"unsupported-{index}"
            cells.append(
                {
                    "cell_id": f"cell-{index}",
                    "case_builder": "unsupported_builder",
                    "search_cell": identity,
                    "seeds": list(range(first_seed, first_seed + 64)),
                }
            )
        calls = []

        def fake_builder(seed, **kwargs):
            calls.append((seed, kwargs))
            return base

        with tempfile.TemporaryDirectory() as directory:
            _build_upper_kara_case_cached_v1.cache_clear()
            path = Path(directory) / "cells.json"
            path.write_text(
                json.dumps({"schema": MANIFEST_SCHEMA, "cells": cells}),
                encoding="utf-8",
            )
            bridge, bridge_cwd, _ = self._paths(directory)
            with patch(
                "o2o_dps.wave_action_sequence_remote_worker_v1."
                "build_upper_kara_exact_cell_case_v1",
                side_effect=fake_builder,
            ):
                plan = plan_wave_action_sequence_shard_v1(
                    cell_manifest=path,
                    shard_index=0,
                    bridge=bridge,
                    bridge_cwd=bridge_cwd,
                )
                repeated = plan_wave_action_sequence_shard_v1(
                    cell_manifest=path,
                    shard_index=0,
                    bridge=bridge,
                    bridge_cwd=bridge_cwd,
                )
            _build_upper_kara_case_cached_v1.cache_clear()
        self.assertEqual("READY_UPPER_KARA_EXACT_CELLS", plan["status"])
        self.assertEqual(plan["cells"], repeated["cells"])
        self.assertEqual("READY_UPPER_KARA_EXACT_CELL", plan["cells"][0]["status"])
        self.assertEqual(64, len(calls))
        self.assertEqual(
            list(range(first_seed, first_seed + 64)),
            [seed for seed, _ in calls],
        )
        self.assertTrue(all(
            kwargs["representative_rank"] == 7
            and kwargs["stratum"] == "q05"
            for _, kwargs in calls
        ))

    def test_manifest_guard_options_override_default_grid(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = _manifest(directory)
            document = json.loads(manifest.read_text(encoding="utf-8"))
            document["cells"][0]["guard_options"] = []
            manifest.write_text(json.dumps(document), encoding="utf-8")
            bridge, bridge_cwd, output = self._paths(directory)
            result = run_wave_action_sequence_shard_v1(
                cell_manifest=manifest,
                shard_index=0,
                bridge=bridge,
                bridge_cwd=bridge_cwd,
                output_dir=output,
                summary=Path(directory) / "summary.json",
                cell_runner=_fake_pilot,
            )
            terminal = json.loads(
                (output / "cell-0.json").read_text(encoding="utf-8")
            )
        self.assertEqual("DONE", result["status"])
        self.assertEqual([], terminal["resolved_guard_options"])
        self.assertEqual(
            [
                ObservableCausalGuardV1(rage_gte=30).to_dict(),
                ObservableCausalGuardV1(
                    target_index=0, target_hp_pct_lte=20
                ).to_dict(),
            ],
            terminal["worker_config"]["guard_options"],
        )

    def test_upper_eval_dry_run_rebuilds_and_identifies_both_seed_panels(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, base = _upper_eval_manifest(directory)
            bridge, bridge_cwd, _ = self._paths(directory)
            calls = []

            def fake_builder(seed, **kwargs):
                calls.append(seed)
                return base

            _build_upper_kara_case_cached_v1.cache_clear()
            with patch(
                "o2o_dps.wave_action_sequence_remote_worker_v1."
                "build_upper_kara_exact_cell_case_v1",
                side_effect=fake_builder,
            ):
                plan = plan_wave_action_sequence_shard_v1(
                    cell_manifest=manifest,
                    shard_index=0,
                    bridge=bridge,
                    bridge_cwd=bridge_cwd,
                )
            _build_upper_kara_case_cached_v1.cache_clear()

        cell = plan["cells"][0]
        self.assertEqual("READY_UPPER_KARA_EXACT_CELL", cell["status"])
        self.assertEqual("train_eval", cell["terminal_kind"])
        self.assertEqual(64, cell["train_seed_count"])
        self.assertEqual(64, cell["evaluation_seed_count"])
        self.assertEqual(128, cell["identity_seed_count"])
        self.assertEqual(64, len(cell["seed_panels"]["train"]))
        self.assertEqual(64, len(cell["seed_panels"]["evaluation"]))
        self.assertEqual(128, len(calls))

    def test_default_upper_eval_runner_forwards_relocated_catalogs(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, base = _upper_eval_manifest(directory)
            bridge, bridge_cwd, _ = self._paths(directory)

            _build_upper_kara_case_cached_v1.cache_clear()
            with patch(
                "o2o_dps.wave_action_sequence_remote_worker_v1."
                "build_upper_kara_exact_cell_case_v1",
                return_value=base,
            ):
                plan = plan_wave_action_sequence_shard_v1(
                    cell_manifest=manifest,
                    shard_index=0,
                    bridge=bridge,
                    bridge_cwd=bridge_cwd,
                    item_database="/compact/db.json",
                    selector_manifest="/compact/selector/manifest.json",
                    selector_representatives="/compact/selector/representatives.jsonl",
                    catalog_manifest="/compact/catalog/manifest.json",
                    catalog_data="/compact/catalog/catalog.jsonl.gz",
                )
                cell = load_exact_cell_manifest_v1(manifest)["cells"][0]
                with patch(
                    "o2o_dps.wave_action_sequence_remote_worker_v1."
                    "run_upper_kara_exact_train_eval_cell_v1",
                    return_value={"schema": "fixture/train_eval"},
                ) as train_eval:
                    result = _default_cell_runner(cell, plan["worker_config"])
            _build_upper_kara_case_cached_v1.cache_clear()

        self.assertEqual({"schema": "fixture/train_eval"}, result)
        kwargs = train_eval.call_args.kwargs
        self.assertEqual(tuple(cell["seeds"]), kwargs["train_seeds"])
        self.assertEqual(
            tuple(cell["evaluation_seeds"]), kwargs["evaluation_seeds"]
        )
        self.assertEqual(Path("/compact/db.json"), kwargs["item_database_path"])
        self.assertEqual(
            Path("/compact/selector/manifest.json"),
            kwargs["selector_manifest_path"],
        )
        self.assertEqual(
            {
                "representatives": str(
                    Path("/compact/selector/representatives.jsonl")
                ),
                "catalog_manifest": str(
                    Path("/compact/catalog/manifest.json")
                ),
                "catalog_data": str(
                    Path("/compact/catalog/catalog.jsonl.gz")
                ),
            },
            kwargs["profile_path_overrides"],
        )

    def test_upper_eval_terminal_is_marked_and_bound_to_both_seed_panels(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, base = _upper_eval_manifest(directory)
            bridge, bridge_cwd, output = self._paths(directory)

            def fake_train_eval(cell, _config):
                return {
                    "schema": "upper_kara_exact_train_eval/v1",
                    "status": "INCOMPLETE_TRAINING_NO_HELD_OUT_EVALUATION",
                    "search_cell": deepcopy(cell["search_cell"]),
                    "training": {
                        "master_seeds": list(cell["seeds"]),
                        "search": {"cell": deepcopy(cell["search_cell"])},
                    },
                    "evaluation": {
                        "executed": False,
                        "master_seeds": list(cell["evaluation_seeds"]),
                    },
                }

            _build_upper_kara_case_cached_v1.cache_clear()
            with patch(
                "o2o_dps.wave_action_sequence_remote_worker_v1."
                "build_upper_kara_exact_cell_case_v1",
                return_value=base,
            ):
                result = run_wave_action_sequence_shard_v1(
                    cell_manifest=manifest,
                    shard_index=0,
                    bridge=bridge,
                    bridge_cwd=bridge_cwd,
                    output_dir=output,
                    summary=Path(directory) / "summary.json",
                    cell_runner=fake_train_eval,
                )
            terminal = json.loads(
                (output / "cell-0.json").read_text(encoding="utf-8")
            )
            _build_upper_kara_case_cached_v1.cache_clear()

        self.assertEqual("DONE", result["status"])
        self.assertEqual("train_eval", terminal["terminal_kind"])
        self.assertEqual(64, len(terminal["train_seeds"]))
        self.assertEqual(64, len(terminal["evaluation_seeds"]))
        self.assertEqual(
            "INCOMPLETE_TRAINING_NO_HELD_OUT_EVALUATION",
            terminal["pilot"]["status"],
        )


if __name__ == "__main__":
    unittest.main()
