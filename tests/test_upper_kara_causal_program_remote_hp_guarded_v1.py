from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from o2o_dps.upper_kara_cat_hp_guarded_sparse_routing_search_v1 import (
    HP_GUARDED_SPARSE_ROUTING_PROGRAM_COUNT_V1,
    UpperKaraCatHpGuardedSparseRoutingGeneratorV1,
)
from o2o_dps.upper_kara_causal_program_remote_contract_v1 import (
    FIXED_PARENT_CAT_HP_GUARDED_SPARSE_ROUTING_KIND,
    FixedParentCatHpGuardedSparseRoutingSearchV1,
    campaign_from_dict_v1,
    load_continuous_two_wave_remote_campaign_v1,
)
from o2o_dps.upper_kara_causal_program_remote_worker_v1 import (
    RemoteCausalProgramRuntimeV1,
    _paired_zero_program_v1,
    _shared_burst_control_programs_v1,
    build_default_remote_runtime_v1,
)
from o2o_dps.upper_kara_causal_program_remote_worker_v2 import (
    generate_candidate_manifest_v2,
)
from o2o_dps.upper_kara_two_wave_train_eval_v1 import (
    imported_incumbent_programs_v1,
)
from scripts.upper_kara_causal_program_remote_submit_v1 import (
    build_upper_kara_causal_program_remote_plan_v2,
)
from tests.test_upper_kara_cat_hp_guarded_sparse_routing_search_v1 import (
    LOADOUT,
    V5_CONFIG,
    _prototype_panel,
)


class HpGuardedRemoteIntegrationV1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        v5 = load_continuous_two_wave_remote_campaign_v1(V5_CONFIG)
        cls.parent = v5.search_spec.parent_program
        cls.prototypes_by_index = _prototype_panel(cls.parent)
        cls.spec = FixedParentCatHpGuardedSparseRoutingSearchV1(
            parent_loadout_id=LOADOUT,
            parent_program=cls.parent,
            prototype_programs=tuple(
                cls.prototypes_by_index[index]
                for index in (42, 47, 159, 262)
            ),
        )
        cls.campaign = replace(
            v5,
            campaign_id="hp-guarded-remote-integration-test",
            search_spec=cls.spec,
        )

    def test_strict_search_spec_round_trip_and_receipt_identity(self) -> None:
        wire = self.campaign.to_dict()
        search = wire["search_spec"]
        self.assertEqual(
            FIXED_PARENT_CAT_HP_GUARDED_SPARSE_ROUTING_KIND,
            search["kind"],
        )
        self.assertEqual(309, search["max_programs"])
        self.assertEqual([20, 35, 50, 65, 80], search["hp_thresholds"])
        self.assertEqual(
            [42, 47, 159, 262],
            [row["prototype_index"] for row in search["prototype_programs"]],
        )
        for row in search["prototype_programs"]:
            self.assertEqual(row["program_ref"], row["program_id"])
            self.assertEqual(row["program_id"], row["program"]["program_id"])
            self.assertEqual("SEARCHED", row["program_origin"])
        parsed = campaign_from_dict_v1(wire)
        self.assertEqual(wire, parsed.to_dict())

    def test_prototype_index_id_key_and_selector_drift_fail_closed(self) -> None:
        base = self.campaign.to_dict()
        mutations = []

        wrong_index = deepcopy(base)
        wrong_index["search_spec"]["prototype_programs"][0][
            "prototype_index"
        ] = 47
        mutations.append(wrong_index)

        wrong_id = deepcopy(base)
        wrong_id["search_spec"]["prototype_programs"][0]["program_id"] += "-drift"
        mutations.append(wrong_id)

        wrong_key = deepcopy(base)
        wrong_key["search_spec"]["prototype_programs"][0]["program_key"] += "-drift"
        mutations.append(wrong_key)

        wrong_selector = deepcopy(base)
        wrong_selector["search_spec"]["prototype_programs"][0]["program"][
            "selector"
        ]["block_alternatives"][0]["guard"]["all_of"][
            "target_hp_pct_lte"
        ] = {"target_index": 0, "percent": 50}
        # Keep the duplicated receipt metadata internally consistent; the v6
        # prototype selector contract must still reject the semantic drift.
        mutated_program = wrong_selector["search_spec"]["prototype_programs"][0][
            "program"
        ]
        from o2o_dps.causal_action_program_v1 import (
            causal_action_program_from_dict_v1,
        )

        parsed_program = causal_action_program_from_dict_v1(mutated_program)
        receipt = wrong_selector["search_spec"]["prototype_programs"][0]
        receipt["program_key"] = parsed_program.program_key()
        mutations.append(wrong_selector)

        missing = deepcopy(base)
        missing["search_spec"]["prototype_programs"].pop()
        mutations.append(missing)

        for position, wire in enumerate(mutations):
            with self.subTest(position=position):
                with self.assertRaises(ValueError):
                    campaign_from_dict_v1(wire)

    def test_default_runtime_dispatches_hp_generator_and_reuses_parent(self) -> None:
        with patch(
            "o2o_dps.upper_kara_imported_incumbent_program_v1."
            "build_imported_incumbent_program_replay_factory_v1",
            return_value=lambda *_args, **_kwargs: None,
        ):
            runtime = build_default_remote_runtime_v1(
                self.campaign,
                bridge_path="bridge",
                bridge_cwd="wowsims",
                runtime_binding_path="binding.json",
                offline_guide_artifact_path="guide.json",
            )
        self.assertIsInstance(
            runtime.searched_program_generator,
            UpperKaraCatHpGuardedSparseRoutingGeneratorV1,
        )
        programs = runtime.searched_program_generator(
            loadout_id=LOADOUT,
            train_examples=(),
            train_cases=(),
        )
        self.assertEqual(HP_GUARDED_SPARSE_ROUTING_PROGRAM_COUNT_V1, len(programs))
        self.assertIs(self.parent, programs[0])
        self.assertIs(self.parent, _paired_zero_program_v1(self.campaign, LOADOUT))
        controls = _shared_burst_control_programs_v1(
            self.campaign,
            imported_incumbent_programs_v1(self.campaign.build_id),
        )
        self.assertEqual(3, len(controls))
        self.assertTrue(
            all(
                program.selector.terminal_alternatives
                == self.parent.selector.terminal_alternatives
                for _, program in controls
            )
        )

    def test_compact_manifest_and_planner_use_exact_312_program_count(self) -> None:
        with TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            campaign_path = root / "campaign.json"
            campaign_path.write_text(
                json.dumps(self.campaign.to_dict(), ensure_ascii=False),
                encoding="utf-8",
            )

            def case_builder(seed: int, **_: object) -> object:
                return SimpleNamespace(
                    dynamic_load=SimpleNamespace(
                        request_sha256=hashlib.sha256(
                            f"case-{seed}".encode("ascii")
                        ).hexdigest()
                    ),
                )

            generator = UpperKaraCatHpGuardedSparseRoutingGeneratorV1(
                loadout_id=LOADOUT,
                parent_program=self.parent,
                prototype_programs=self.prototypes_by_index,
            )
            runtime = RemoteCausalProgramRuntimeV1(
                case_builder=case_builder,
                searched_program_generator=generator,
                program_replay_factory=lambda *_: None,
            )
            manifest = generate_candidate_manifest_v2(
                campaign_path=campaign_path,
                loadout_id=LOADOUT,
                output_path=root / "candidate.json",
                bridge_path=root,
                bridge_cwd=root,
                runtime_binding_path=root,
                offline_guide_artifact_path=root,
                runtime=runtime,
            )
            self.assertEqual(312, len(manifest["programs"]))

            plan = build_upper_kara_causal_program_remote_plan_v2(
                shared_home="/home/tester",
                run_id="hp-guarded-plan-test",
                phase="train",
                campaign=campaign_path,
                bridge_name="bridge-v23.linux-amd64",
            )
        audit = plan["candidate_count_audit"]
        self.assertEqual(309, audit["searched_hp_guarded_sparse_routing_count"])
        self.assertEqual(312, audit["total_training_program_upper_bound"])
        self.assertTrue(audit["exact_unique_count_known_at_plan_time"])
        self.assertFalse(audit["projection_may_deduplicate"])
        self.assertTrue(audit["fixed_parent_burst_program_held_constant"])
        self.assertEqual(
            312 * len(self.campaign.train_examples),
            plan["phase_logical_item_count"],
        )


if __name__ == "__main__":
    unittest.main()
