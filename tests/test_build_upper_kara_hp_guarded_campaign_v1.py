from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.causal_action_program_v1 import (
    CausalActionProgramV1,
    GuardedAlternativeV1,
    ImportedReactiveBurstQueueGcdBlockSelectorV1,
    ProgramDecisionV1,
    ProgramOriginV1,
)
from o2o_dps.causal_guard_v1 import ObservableCausalGuardV1, SKIP_PLAN
from o2o_dps.upper_kara_cat_hp_guarded_sparse_routing_search_v1 import (
    V5_PROTOTYPE_INDEXES_V1,
    _expected_prototype_signatures_v1,
)
from o2o_dps.upper_kara_causal_program_remote_contract_v1 import (
    assign_training_shards_v1,
    campaign_from_dict_v1,
    load_continuous_two_wave_remote_campaign_v1,
)
from o2o_dps.wave_action_schedule_v1 import QueueLaneOp
from scripts.build_upper_kara_hp_guarded_campaign_v1 import (
    HP_ROUTING_SEARCH_KIND,
    PROTOTYPE_REGISTRY_SCHEMA,
    TRAIN_TERMINAL_SCHEMA,
    V6_PROGRAM_COUNT,
    build_hp_guarded_campaign_wire_v1,
    campaign_contract_v1,
    extract_prototype_registry_v1,
    load_prototype_registry_v1,
    write_json_create_only_v1,
)


V5_CAMPAIGN = (
    PROJECT_ROOT
    / "configs/evaluation/upper_kara_fixed_burst_block_remote_v5_256x256_v1.json"
)


def _prototype(
    parent: CausalActionProgramV1,
    *,
    loadout_id: str,
    index: int,
) -> CausalActionProgramV1:
    parent_selector = parent.selector
    alternatives: list[GuardedAlternativeV1] = []
    for (
        alternative_id,
        target_index,
        action,
        queue_op,
        rage_gte,
        queue_status_is,
        wait_ms,
    ) in _expected_prototype_signatures_v1()[index]:
        guard = ObservableCausalGuardV1(
            rage_gte=rage_gte,
            target_index=target_index,
            target_attackable_is=True,
            queue_status_is=queue_status_is,
            action_ready=action,
            false_semantics=SKIP_PLAN,
        )
        if queue_op is QueueLaneOp.SET:
            decision = ProgramDecisionV1(
                queue_op=queue_op,
                queue_action=action,
                wait_ms=wait_ms,
            )
        else:
            decision = ProgramDecisionV1(
                queue_op=queue_op,
                gcd_action=action,
            )
        alternatives.append(
            GuardedAlternativeV1(alternative_id, guard, decision)
        )
    return CausalActionProgramV1(
        program_id=(
            f"cat-burst-sparse-queue-gcd::{loadout_id}::{index:04d}"
        ),
        selector=ImportedReactiveBurstQueueGcdBlockSelectorV1(
            terminal_alternatives=parent_selector.terminal_alternatives,
            imported_fallback=parent_selector.imported_fallback,
            block_alternatives=tuple(alternatives),
            off_gcd_insertions=parent_selector.off_gcd_insertions,
            insertion_order=parent_selector.insertion_order,
            insertion_position=parent_selector.insertion_position,
        ),
        origin=ProgramOriginV1.SEARCHED,
        source_refs=(
            *parent.source_refs,
            "cat-burst-sparse-queue-gcd/v1:projected",
            f"fixture-v5-prototype:{index}",
        ),
    )


def _receipt(program: CausalActionProgramV1) -> dict:
    return {
        "program_ref": program.program_id,
        "program_id": program.program_id,
        "program_key": program.program_key(),
        "program_origin": program.origin.value,
        "proposal_guide_ids": [
            source_ref.removeprefix("proposal-guide:")
            for source_ref in program.source_refs
            if source_ref.startswith("proposal-guide:")
        ],
        "program": program.to_dict(),
    }


def _terminal(campaign) -> dict:
    spec = campaign.search_spec
    shard = assign_training_shards_v1(campaign)[0]
    prototypes = [
        _prototype(
            spec.parent_program,
            loadout_id=spec.parent_loadout_id,
            index=index,
        )
        for index in V5_PROTOTYPE_INDEXES_V1
    ]
    return {
        "schema": TRAIN_TERMINAL_SCHEMA,
        "terminal_status": "COMPLETE",
        "campaign_id": campaign.campaign_id,
        "build_id": campaign.build_id,
        "campaign_contract": campaign_contract_v1(campaign),
        "loadout_id": spec.parent_loadout_id,
        "seed_shard_index": shard.seed_shard_index,
        "examples": [row.to_dict() for row in shard.examples],
        "programs": [_receipt(spec.parent_program)]
        + [_receipt(program) for program in prototypes],
    }


def _write(path: Path, value: dict) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )


class BuildUpperKaraHpGuardedCampaignV1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.v5 = load_continuous_two_wave_remote_campaign_v1(V5_CAMPAIGN)

    def _registry_path(self, root: Path) -> tuple[Path, dict]:
        terminal_path = root / "terminal.json"
        _write(terminal_path, _terminal(self.v5))
        registry = extract_prototype_registry_v1(
            v5_campaign_path=V5_CAMPAIGN,
            train_terminal_path=terminal_path,
        )
        registry_path = root / "prototype-registry.json"
        write_json_create_only_v1(registry_path, registry)
        return registry_path, registry

    def test_extracts_exact_ordered_receipts_and_recomputes_identity(self) -> None:
        with TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            registry_path, registry = self._registry_path(root)
            self.assertEqual(PROTOTYPE_REGISTRY_SCHEMA, registry["schema"])
            self.assertEqual(
                list(V5_PROTOTYPE_INDEXES_V1),
                [row["prototype_index"] for row in registry["prototypes"]],
            )
            self.assertEqual(
                [
                    "prototype_index",
                    "program_ref",
                    "program_id",
                    "program_key",
                    "program_origin",
                    "proposal_guide_ids",
                    "program",
                ],
                list(registry["prototypes"][0]),
            )
            self.assertEqual(
                registry,
                load_prototype_registry_v1(
                    registry_path, v5_campaign=self.v5
                ),
            )

    def test_builds_fresh_256x256_campaign_with_exact_hp_search_spec(self) -> None:
        with TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            registry_path, registry = self._registry_path(root)
            wire = build_hp_guarded_campaign_wire_v1(
                v5_campaign_path=V5_CAMPAIGN,
                prototype_registry_path=registry_path,
            )
            self.assertEqual(256, len(wire["train_examples"]))
            self.assertEqual(256, len(wire["evaluation_examples"]))
            self.assertEqual(720_001, wire["train_examples"][0]["seed"])
            self.assertEqual(720_256, wire["train_examples"][-1]["seed"])
            self.assertEqual(820_001, wire["evaluation_examples"][0]["seed"])
            self.assertEqual(820_256, wire["evaluation_examples"][-1]["seed"])
            self.assertEqual(256, wire["seed_shard_count"])
            arrivals = Counter(
                row["first_wave_arrival_ms"] for row in wire["train_examples"]
            )
            self.assertEqual(
                {0, 1_000, 3_000, 5_000, 7_000, 9_000}, set(arrivals)
            )
            self.assertEqual({42, 43}, set(arrivals.values()))
            self.assertEqual(self.v5.build_id, wire["build_id"])
            self.assertEqual(
                self.v5.generation_config.to_dict(), wire["generation_config"]
            )
            self.assertEqual(self.v5.pull_time_ms, wire["pull_time_ms"])
            self.assertEqual(self.v5.max_decisions, wire["max_decisions"])
            spec = wire["search_spec"]
            self.assertEqual(HP_ROUTING_SEARCH_KIND, spec["kind"])
            self.assertEqual(V6_PROGRAM_COUNT, spec["max_programs"])
            self.assertEqual([20, 35, 50, 65, 80], spec["hp_thresholds"])
            self.assertEqual(registry["prototypes"], spec["prototype_programs"])
            self.assertNotIn(
                "first_wave_arrival_ms",
                json.dumps(spec, ensure_ascii=True, sort_keys=True),
            )
            self.assertTrue(
                wire["contract"][
                    "search_routes_exact_v5_prototypes_by_current_hp"
                ]
            )
            self.assertIs(
                False, wire["contract"]["first_wave_arrival_visible_to_policy"]
            )
            self.assertEqual(
                V6_PROGRAM_COUNT,
                wire["contract"]["exact_searched_program_count"],
            )
            self.assertEqual(wire, campaign_from_dict_v1(wire).to_dict())

    def test_tampered_receipt_and_reordered_registry_fail_closed(self) -> None:
        with TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            terminal = _terminal(self.v5)
            terminal["programs"][1]["program_key"] = "tampered"
            terminal_path = root / "bad-terminal.json"
            _write(terminal_path, terminal)
            with self.assertRaisesRegex(ValueError, "identity mismatch"):
                extract_prototype_registry_v1(
                    v5_campaign_path=V5_CAMPAIGN,
                    train_terminal_path=terminal_path,
                )

            registry_path, registry = self._registry_path(root)
            reordered = deepcopy(registry)
            reordered["prototypes"][0], reordered["prototypes"][1] = (
                reordered["prototypes"][1],
                reordered["prototypes"][0],
            )
            reordered_path = root / "reordered.json"
            _write(reordered_path, reordered)
            with self.assertRaisesRegex(ValueError, "order/index"):
                load_prototype_registry_v1(
                    reordered_path, v5_campaign=self.v5
                )

    def test_registry_is_bound_to_the_complete_source_terminal(self) -> None:
        with TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            terminal = _terminal(self.v5)
            terminal["terminal_status"] = "FAILED"
            terminal_path = root / "failed-terminal.json"
            _write(terminal_path, terminal)
            with self.assertRaisesRegex(ValueError, "identity"):
                extract_prototype_registry_v1(
                    v5_campaign_path=V5_CAMPAIGN,
                    train_terminal_path=terminal_path,
                )

    def test_output_publication_is_create_only(self) -> None:
        with TemporaryDirectory() as raw_root:
            destination = Path(raw_root) / "artifact.json"
            write_json_create_only_v1(destination, {"value": 1})
            with self.assertRaises(FileExistsError):
                write_json_create_only_v1(destination, {"value": 2})
            self.assertEqual({"value": 1}, json.loads(destination.read_text()))


if __name__ == "__main__":
    unittest.main()
