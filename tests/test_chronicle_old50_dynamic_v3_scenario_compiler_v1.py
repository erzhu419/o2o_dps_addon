from __future__ import annotations

from copy import deepcopy
import json
import unittest
from unittest import mock

from o2o_dps import chronicle_external_team_background_generator_v2 as background_v2
from o2o_dps import chronicle_stage6_old50_overlap_hpc_v1 as overlap_v1
from o2o_dps.chronicle_old50_dynamic_v3_scenario_compiler_v1 import (
    ChronicleOld50DynamicV3CompilerError,
    compile_old50_dynamic_v3_scenario_v1,
    validate_old50_dynamic_v3_scenario_v1,
)
from o2o_dps.fury_encounter_scenarios_v1 import ARMOR_STAT_INDEX, HEALTH_STAT_INDEX
from tests.test_fury_capsule_execution_binding_v2 import (
    _base_request,
    _bundle as _capsule_bundle,
    _reseal_scenario_and_bundle,
)


FOCAL_GUID = "0x00000000000000F1"
TEAMMATE_GUID = "0x00000000000000A2"
SELECTED_TARGET_GUID = "0xF130000000"
OTHER_TARGET_GUID = "0xF130000099"
STAGE6_COMPONENT = "d" * 64


def _addressed(core: dict[str, object]) -> dict[str, object]:
    return overlap_v1._content_addressed(core)


def _compiler_input(
    *, bundle: dict[str, object], block: dict[str, object]
) -> dict[str, object]:
    scenario = bundle["scenarios"][0]
    source = scenario["source_identity"]
    remap = [
        {
            "target_guid": SELECTED_TARGET_GUID,
            "stage6_target_index": 1,
            "capsule_target_index": 0,
        }
    ]
    core = {
        "schema": overlap_v1.COMPILER_INPUT_SCHEMA,
        "revision": overlap_v1.REVISION,
        "status": "READY_FOR_HYPOTHESIS_SELECTION_NONVOTING",
        "identity": {
            "instance_id": source["instance_id"],
            "encounter_id": source["encounter_id"],
            "wave_id": source["wave_id"],
            "wave_ordinal": source["wave_ordinal"],
            "scenario_id": scenario["scenario_id"],
            "focal_player_guid": FOCAL_GUID,
        },
        "source_bindings": {
            "old50_capsule": {
                "locator": "fixture/capsule.json.gz",
                "bundle_content_sha256": bundle["content_address"]["sha256"],
                "scenario_id": scenario["scenario_id"],
                "scenario_capsule_sha256": scenario["capsule_sha256"],
                "wave_id": source["wave_id"],
            },
            "stage5_exact_wave": {
                "manifest_locator": "fixture/stage5.json",
                "manifest_content_sha256": "5" * 64,
                "partition_locator": "fixture/stage5.jsonl.gz",
                "wave_content_sha256": "6" * 64,
                "purpose": "exact Fury focal GUID evidence only",
            },
            "stage6_exact_block": {
                "manifest_locator": "fixture/stage6.json",
                "manifest_content_sha256": "7" * 64,
                "partition_locator": "fixture/stage6.jsonl.gz",
                "block_content_sha256": block["content_address"]["sha256"],
                "wave_id": source["wave_id"],
            },
        },
        "join": {
            "classification": "SUBSET",
            "target_guid_relation": "STRICT_SUBSET",
            "selected_target_guids": [SELECTED_TARGET_GUID],
            "target_index_remap": remap,
            "component_relation": {
                "old50_component_id": "component",
                "stage6_component_id": STAGE6_COMPONENT,
                "comparison_universe": "fixture",
                "old50_equivalence_class": [source["instance_id"]],
                "stage6_equivalence_class": [source["instance_id"]],
                "relation": "OVERLAP_PROJECTION_EXACT",
                "raw_component_id_equality_required": False,
                "split_authority": "EQUIVALENT_ON_OVERLAP",
            },
            "contamination": {
                "label": "NO_KNOWN_RULE_MATCH",
                "exact_stage5_wave_normalized_lane_equal": True,
            },
        },
        "causal_schedule_projection": {
            "selection_unit": "EXACT_CONTENT_ADDRESSED_STAGE6_BLOCK",
            "component_wide_random_draw_allowed": False,
            "reason_component_draw_forbidden": "another wave can differ",
            "focal_leave_one_out": {
                "predicate": "actor_player_guid != focal_player_guid",
                "focal_player_guid": FOCAL_GUID,
                "exact_guid_only": True,
                "excluded_focal_event_count": 1,
                "excluded_focal_damage_amount": 30,
            },
            "target_projection": {
                "predicate": "target_guid in selected_target_guids",
                "selected_target_guids": [SELECTED_TARGET_GUID],
                "unselected_target_events_enter_selected_targets": False,
                "stage6_to_capsule_target_index_remap": remap,
            },
            "time_ms_preserved": True,
            "source_eventmeta_order_preserved": True,
            "same_timestamp_order_preserved": True,
            "capsule_horizon_ms": scenario["horizon"]["milliseconds"],
            "selected_events_outside_horizon": 0,
        },
        "dynamic_v3_compile_requirements": {
            "command": "load_dynamic_v3",
            "dynamic_config_schema": "o2o_dynamic_target_semantics/v3",
            "target_health_hypothesis_required_for_every_selected_guid": True,
            "target_armor_hypothesis_required_for_every_selected_guid": True,
            "attackability_hypothesis_required_for_every_selected_guid": True,
            "observed_kill_or_damage_budget_may_be_exact_hp": False,
            "observed_activity_may_be_exact_attackability": False,
            "hypothesis_selection_completed": False,
            "load_dynamic_v3_wire_ready": False,
        },
        "scientific_boundary": {
            "historical_truth": False,
            "diagnostic_nonvoting": True,
            "comparison_ready": False,
            "formal_runner_registered": False,
        },
    }
    result = _addressed(core)
    overlap_v1.validate_compiler_input(result)
    return result


def _fixture() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    bundle = _capsule_bundle()
    scenario = bundle["scenarios"][0]
    target = scenario["targets"][0]
    horizon_ms = scenario["horizon"]["milliseconds"]
    scenario["layout"] = {
        "spatial_assumption": {
            "value": "single_target",
            "status": "RECONSTRUCTED",
            "coordinates": None,
            "not_observed": True,
        }
    }
    for branch in target["attackable_window_hypothesis_family"]:
        if branch["branch_id"] == "observed_hostile_activity_proxy":
            branch["windows"] = [[100, 200], [400, horizon_ms]]
    target["armor"]["observed_transitions"] = [
        {
            "debuff_id": "expose_armor",
            "observed_aura_name": "Expose Armor",
            "observed_spell_id": None,
            "spell_id_status": "MISSING_IN_COMPACT_FEATURE_V1",
            "operation": "set",
            "stacks": 1,
            "offset_ms": 300,
            "status": "OBSERVED_AURA_TRANSITION",
            "anchor": {"type": "AURA", "offset_ms": 300, "event_index": 9},
        }
    ]
    _reseal_scenario_and_bundle(bundle, 0)
    source = scenario["source_identity"]
    block_core = {
        "wave": {
            "instance_id": source["instance_id"],
            "encounter_id": source["encounter_id"],
            "encounter_ordinal": 1,
            "wave_id": source["wave_id"],
            "wave_ordinal": source["wave_ordinal"],
        },
        "component_id": STAGE6_COMPONENT,
        "source_model": {
            "wave_content_sha256": "8" * 64,
            "exact_trace_content_sha256": "9" * 64,
        },
        "contamination_lane": {
            "label": "NO_KNOWN_RULE_MATCH",
            "training_candidate": True,
        },
        "roster_player_guids": [FOCAL_GUID, TEAMMATE_GUID],
        "target_registry": [
            {"target_guid": OTHER_TARGET_GUID, "target_index": 0},
            {"target_guid": SELECTED_TARGET_GUID, "target_index": 1},
        ],
        "runtime_candidate_schedule": [
            {
                "target_guid": SELECTED_TARGET_GUID,
                "target_index": 1,
                "actor_player_guid": FOCAL_GUID,
                "time_ms": 110,
                "event_id": "selected-focal",
                "damage": 30,
            },
            {
                "target_guid": SELECTED_TARGET_GUID,
                "target_index": 1,
                "actor_player_guid": TEAMMATE_GUID,
                "time_ms": 120,
                "event_id": "selected-teammate",
                "damage": 50,
            },
            {
                "target_guid": OTHER_TARGET_GUID,
                "target_index": 0,
                "actor_player_guid": TEAMMATE_GUID,
                "time_ms": 130,
                "event_id": "unselected-target",
                "damage": 70,
            },
        ],
        "nonruntime_damage_diagnostics": [],
        "dead_marker_diagnostics": [],
        "negative_damage_diagnostics": [],
    }
    block = background_v2._content_addressed(block_core)
    return bundle, block, _compiler_input(bundle=bundle, block=block)


def _compile(
    bundle: dict[str, object],
    block: dict[str, object],
    compiler_input: dict[str, object],
):
    with mock.patch.object(background_v2, "_validate_block"):
        return compile_old50_dynamic_v3_scenario_v1(
            compiler_input=compiler_input,
            capsule_bundle=bundle,
            stage6_block=block,
            base_request=_base_request(),
            equipped_item_names=("削骨之刃", "十字军附魔"),
            target_level=63,
            initial_base_armor=4211,
            health_branch_id="proxy_center",
            attackability_branch_id="observed_hostile_activity_proxy",
            target_classification="elite",
            armor_magnitude_by_debuff={"expose_armor": 1700},
        )


class ChronicleOld50DynamicV3ScenarioCompilerV1Tests(unittest.TestCase):
    def test_exact_guid_join_remap_loo_and_causal_policy_context(self) -> None:
        bundle, block, compiler_input = _fixture()
        compiled = _compile(bundle, block, compiler_input)
        validated = validate_old50_dynamic_v3_scenario_v1(compiled.artifact)

        self.assertEqual(
            block["content_address"]["sha256"],
            validated["source_bindings"]["stage6_block_sha256"],
        )
        projection = validated["causal_background_projection"]
        self.assertTrue(projection["exact_guid_leave_one_out"])
        self.assertEqual(1, projection["selected_focal_runtime_event_count_removed"])
        self.assertEqual(30.0, projection["selected_focal_runtime_damage_removed"])
        self.assertEqual(1, projection["kept_background_event_count"])
        self.assertEqual(1, projection["unselected_target_runtime_event_count_removed"])
        background = [row.to_wire() for row in compiled.dynamic_config.background_damage_events]
        self.assertEqual(
            [
                {
                    "schedule_index": 0,
                    "time_ms": 120,
                    "target_index": 0,
                    "event_id": "selected-teammate",
                    "damage": 50.0,
                }
            ],
            background,
        )

        attacks = [row.to_wire() for row in compiled.dynamic_config.attackability_events]
        self.assertEqual(
            [
                {"schedule_index": 0, "time_ms": 0, "target_index": 0, "attackable": False},
                {"schedule_index": 1, "time_ms": 100, "target_index": 0, "attackable": True},
                {"schedule_index": 2, "time_ms": 201, "target_index": 0, "attackable": False},
                {"schedule_index": 3, "time_ms": 400, "target_index": 0, "attackable": True},
            ],
            attacks,
        )
        self.assertTrue(
            validated["hypothesis_selection"]["attackability"][
                "inclusive_end_compiled_as_false_at_end_plus_one_ms"
            ]
        )

        self.assertEqual(
            [{"target_index": 0, "health": 10_000.0}],
            [row.to_wire() for row in compiled.dynamic_config.target_health],
        )
        request_target = compiled.scenario["request"]["encounter"]["targets"][0]
        self.assertEqual(10_000, request_target["stats"][HEALTH_STAT_INDEX])
        self.assertEqual(4211, request_target["stats"][ARMOR_STAT_INDEX])
        context = compiled.scenario["target_context_bundle"]["contexts"][0]
        self.assertEqual(["削骨之刃", "十字军附魔"], context["equipped_item_names"])
        self.assertEqual([], context["health_pct_schedule"])
        context_json = json.dumps(
            compiled.scenario["target_context_bundle"], ensure_ascii=False
        )
        self.assertNotIn("runtime_candidate_schedule", context_json)
        self.assertNotIn("selected-teammate", context_json)
        self.assertFalse(
            validated["scientific_boundary"][
                "future_background_schedule_visible_to_policy"
            ]
        )

    def test_ambiguous_expose_armor_requires_an_explicit_branch(self) -> None:
        bundle, block, compiler_input = _fixture()
        with (
            mock.patch.object(background_v2, "_validate_block"),
            self.assertRaisesRegex(
                ChronicleOld50DynamicV3CompilerError,
                "armor debuff expose_armor is ambiguous",
            ),
        ):
            compile_old50_dynamic_v3_scenario_v1(
                compiler_input=compiler_input,
                capsule_bundle=bundle,
                stage6_block=block,
                base_request=_base_request(),
                equipped_item_names=("削骨之刃",),
                target_level=63,
                initial_base_armor=4211,
                health_branch_id="proxy_center",
                attackability_branch_id="observed_hostile_activity_proxy",
                target_classification="elite",
            )
        compiled = _compile(bundle, block, compiler_input)
        armor = [row.to_wire() for row in compiled.dynamic_config.effective_armor_events]
        self.assertEqual(4211.0, armor[0]["effective_armor"])
        self.assertEqual(2511.0, armor[1]["effective_armor"])
        self.assertEqual(
            [1721.0],
            compiled.artifact["hypothesis_selection"][
                "capsule_legacy_static_base_armor_values"
            ],
        )
        self.assertFalse(
            compiled.artifact["hypothesis_selection"][
                "capsule_legacy_static_armor_reused_implicitly"
            ]
        )
        selected = compiled.artifact["hypothesis_selection"]["armor"][
            "selected_magnitudes"
        ]["expose_armor"]
        self.assertEqual(1700, selected["value"])

    def test_block_content_and_focal_identity_mismatches_fail_closed(self) -> None:
        bundle, block, compiler_input = _fixture()

        wrong_binding = deepcopy(compiler_input)
        wrong_binding["source_bindings"]["stage6_exact_block"][
            "block_content_sha256"
        ] = "f" * 64
        wrong_binding = _addressed(
            {key: value for key, value in wrong_binding.items() if key != "content_address"}
        )
        with (
            mock.patch.object(background_v2, "_validate_block"),
            self.assertRaisesRegex(
                ChronicleOld50DynamicV3CompilerError,
                "content address differs from compiler input",
            ),
        ):
            _compile(bundle, block, wrong_binding)

        tampered_block = deepcopy(block)
        tampered_block["runtime_candidate_schedule"][0]["damage"] = 31
        with (
            mock.patch.object(background_v2, "_validate_block"),
            self.assertRaisesRegex(
                ChronicleOld50DynamicV3CompilerError,
                "invalid Stage-6 exact block",
            ),
        ):
            _compile(bundle, tampered_block, compiler_input)

        focal_mismatch = deepcopy(compiler_input)
        focal_mismatch["causal_schedule_projection"]["focal_leave_one_out"][
            "focal_player_guid"
        ] = TEAMMATE_GUID
        focal_mismatch["causal_schedule_projection"]["focal_leave_one_out"][
            "excluded_focal_damage_amount"
        ] = 50
        focal_mismatch = _addressed(
            {key: value for key, value in focal_mismatch.items() if key != "content_address"}
        )
        with (
            mock.patch.object(background_v2, "_validate_block"),
            self.assertRaisesRegex(
                ChronicleOld50DynamicV3CompilerError,
                "focal.*differs",
            ),
        ):
            _compile(bundle, block, focal_mismatch)


if __name__ == "__main__":
    unittest.main()
