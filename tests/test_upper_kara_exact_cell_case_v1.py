from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from o2o_dps.development_precombat_wave_case_v1 import (
    DevelopmentPrecombatWaveCaseV1,
    MIGHTY_RAGE_POTION_ACTION,
)
from o2o_dps.development_wave_stratified_v1 import WAVE_STRATA
from o2o_dps.factored_external_press_matrix_v1 import STRATUM_BINDINGS
from o2o_dps.fury_dynamic_target_semantics_v5 import (
    validate_dynamic_load_request_v3,
)
from o2o_dps.upper_kara_exact_cell_case_v1 import (
    UPPER_KARA_HISTORICAL_FURY_RANKS,
    UPPER_KARA_WAVE_STRATA,
    build_upper_kara_exact_cell_case_v1,
    build_upper_kara_exact_cell_manifest_v1,
    search_cell_from_upper_kara_case_v1,
)


class UpperKaraExactCellCaseV1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.first_seed = 202_609_140
        cls.manifest = build_upper_kara_exact_cell_manifest_v1(
            cls.first_seed,
            precombat_self_actions=(MIGHTY_RAGE_POTION_ACTION,),
            pull_time_ms=3_000,
        )

    def test_manifest_is_existing_three_by_four_cartesian_product(self) -> None:
        manifest = self.manifest
        self.assertEqual((7, 11, 9), UPPER_KARA_HISTORICAL_FURY_RANKS)
        self.assertEqual(("q05", "q60", "q95", "multi_2"), UPPER_KARA_WAVE_STRATA)
        self.assertEqual(12, manifest["cell_count"])
        self.assertEqual("PREPARED_NOT_RUN", manifest["status"])
        self.assertEqual(
            "ACTUAL_BOUND_FIRST_SEED_CASE",
            manifest["search_cell_identity_source"],
        )
        self.assertEqual(
            {
                (rank, stratum)
                for rank in UPPER_KARA_HISTORICAL_FURY_RANKS
                for stratum in UPPER_KARA_WAVE_STRATA
            },
            {
                (row["representative_rank"], row["stratum"])
                for row in manifest["cells"]
            },
        )
        self.assertEqual(
            12,
            len(
                {
                    json.dumps(row["search_cell"], sort_keys=True)
                    for row in manifest["cells"]
                }
            ),
        )

    def test_manifest_identities_retain_actual_build_resource_and_wave_state(self) -> None:
        for row in self.manifest["cells"]:
            cell = row["search_cell"]
            mechanics = cell["derived_mechanics"]
            exact_build = json.loads(mechanics["exact_build_context_json"])
            initial = json.loads(mechanics["initial_state_json"])
            alias = row["stratum"]
            bound_stratum = STRATUM_BINDINGS[alias]

            self.assertEqual("Upper Tower of Karazhan", row["case_binding"]["source_instance_name"])
            self.assertEqual(WAVE_STRATA[bound_stratum], row["case_binding"]["source_wave_ref"])
            self.assertEqual(bound_stratum, row["bound_stratum"])
            self.assertEqual(self.first_seed, row["first_seed"])
            self.assertEqual(exact_build["talents_string"], "-".join(
                "".join(
                    str(talent["rank"])
                    for talent in cell["talents"]
                    if talent["talent"].startswith(f"tree_{tree}_position_")
                )
                for tree in range(1, 1 + exact_build["talents_string"].count("-") + 1)
            ))
            self.assertEqual(
                mechanics["starting_rage"],
                exact_build["warrior_options"]["startingRage"],
            )
            self.assertEqual(mechanics["starting_rage"], initial["rage"])
            self.assertEqual(
                mechanics["target_count"],
                len(json.loads(mechanics["target_max_hp_json"])),
            )
            self.assertEqual(
                json.loads(mechanics["target_max_hp_json"]),
                json.loads(mechanics["target_current_hp_json"]),
            )
            self.assertEqual(
                mechanics["target_count"],
                len(json.loads(mechanics["target_base_armor_json"])),
            )
            self.assertEqual(
                "MightyRagePotion", exact_build["consumes"]["defaultPotion"]
            )

        multi_rows = [
            row for row in self.manifest["cells"] if row["stratum"] == "multi_2"
        ]
        self.assertEqual(3, len(multi_rows))
        for row in multi_rows:
            mechanics = row["search_cell"]["derived_mechanics"]
            self.assertEqual(2, mechanics["target_count"])
            self.assertEqual(2, len(json.loads(mechanics["target_base_armor_json"])))
            self.assertEqual(2, len(json.loads(mechanics["target_max_hp_json"])))

    def test_exact_cell_precombat_maps_negative_pull_time_and_rebinds(self) -> None:
        case = build_upper_kara_exact_cell_case_v1(
            202_609_141,
            representative_rank=11,
            stratum="multi_2",
            precombat_self_actions=(MIGHTY_RAGE_POTION_ACTION,),
            pull_time_ms=3_000,
        )
        self.assertIsInstance(case, DevelopmentPrecombatWaveCaseV1)
        self.assertEqual(0, case.timeline.to_simulator_time_ms(-3_000))
        self.assertEqual(-3_000, case.timeline.to_pull_relative_time_ms(0))
        self.assertEqual(
            [3_000, 3_000],
            case.case_spec["initial_state"]["target_attackable_at_ms"],
        )
        self.assertEqual(
            "MightyRagePotion",
            case.request["raid"]["parties"][0]["players"][0]["consumes"][
                "defaultPotion"
            ],
        )
        self.assertEqual(
            case.dynamic_load.request_sha256,
            case.case_spec["request_sha256"],
        )
        self.assertEqual(
            case.dynamic_load.contract_sha256,
            case.case_spec["dynamic_load_contract_sha256"],
        )
        validate_dynamic_load_request_v3(case.dynamic_load, case.request)
        identity = search_cell_from_upper_kara_case_v1(case).to_dict()
        self.assertEqual(2, identity["derived_mechanics"]["target_count"])

    def test_one_cell_rebuild_is_reproducible(self) -> None:
        kwargs = {
            "representative_rank": 9,
            "stratum": "q95",
            "precombat_self_actions": (MIGHTY_RAGE_POTION_ACTION,),
            "pull_time_ms": 3_000,
        }
        first = build_upper_kara_exact_cell_case_v1(991, **kwargs)
        second = build_upper_kara_exact_cell_case_v1(991, **kwargs)
        self.assertEqual(
            first.dynamic_load.request_sha256,
            second.dynamic_load.request_sha256,
        )
        self.assertEqual(
            first.dynamic_load.contract_sha256,
            second.dynamic_load.contract_sha256,
        )
        self.assertEqual(
            search_cell_from_upper_kara_case_v1(first).to_dict(),
            search_cell_from_upper_kara_case_v1(second).to_dict(),
        )


if __name__ == "__main__":
    unittest.main()
