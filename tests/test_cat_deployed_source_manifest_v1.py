from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
import shutil
import tempfile
import unittest

from o2o_dps.cat_deployed_source_manifest_v1 import (
    CatDeployedSourceManifestError,
    DEFAULT_MANIFEST,
    EXPECTED_BLOCKERS,
    EXPECTED_CRITICAL_FILES,
    EXPECTED_MANIFEST_SHA256,
    EXPECTED_RUNTIME_KEYS,
    EXPECTED_TOC_CLOSURE,
    EXPECTED_TREE,
    compute_source_tree_facts,
    load_manifest,
    validate_manifest,
    verify_source_tree,
)


ADDONS_ROOT = Path(__file__).resolve().parents[3]
EXTERNAL_DEPLOYED_CAT = ADDONS_ROOT / "Cat"
NESTED_REFERENCE_CAT = ADDONS_ROOT / "BrainOfCat" / "Cat"


class CatDeployedSourceManifestV1Tests(unittest.TestCase):
    def test_manifest_bytes_are_content_addressed(self) -> None:
        self.assertEqual(
            hashlib.sha256(DEFAULT_MANIFEST.read_bytes()).hexdigest(),
            EXPECTED_MANIFEST_SHA256,
        )
        self.assertEqual(load_manifest()["manifest_id"], "cat.deployed_source.1_18_1.c31be2f9")

    def test_source_identity_does_not_claim_runtime_or_comparison(self) -> None:
        manifest = load_manifest()
        authority = manifest["source"]["authority"]
        readiness = manifest["comparison_readiness"]
        self.assertEqual(authority["role"], "SOURCE_IDENTITY_AND_CAPABILITY")
        self.assertFalse(authority["source_execution"])
        self.assertFalse(authority["exact_runtime"])
        self.assertFalse(authority["policy_profile_included"])
        self.assertFalse(authority["eligible_for_comparison"])
        self.assertEqual(readiness["status"], "BLOCKED")
        self.assertTrue(readiness["source_identity_complete"])
        self.assertTrue(readiness["toc_closure_complete"])
        self.assertFalse(readiness["eligible_for_comparison"])
        self.assertEqual(readiness["blockers"], EXPECTED_BLOCKERS)

    def test_fury_entry_profile_and_config_surface_are_explicit(self) -> None:
        surface = load_manifest()["fury_policy_surface"]
        self.assertEqual(
            surface["entry_chain"],
            ["Bindings.xml:WARRIOR_FURY_BINDING", "MPWarriorFuryCommand", "MPFuryDPS"],
        )
        self.assertEqual(surface["slash_command"], "/fury")
        self.assertEqual(surface["saved_variables_scope"], "SavedVariablesPerCharacter")
        self.assertEqual(surface["saved_variables_name"], "MPWarriorFurySaved")
        self.assertEqual(surface["settings_schema_version"], 32)
        self.assertEqual(surface["profile_slot_count"], 3)
        self.assertEqual(surface["binding_selected_profile_slot"], 1)
        self.assertFalse(surface["source_default_is_runtime_profile"])
        self.assertEqual(surface["active_runtime_config_keys"], EXPECTED_RUNTIME_KEYS)
        self.assertEqual(len(surface["active_runtime_config_keys"]), 41)

    def test_return_and_multi_sink_boundaries_are_fail_closed(self) -> None:
        contract = load_manifest()["execution_contract"]
        returns = contract["entry_return_semantics"]
        sinks = contract["sink_semantics"]
        self.assertEqual(returns["MPWarriorFuryCommand"], "NO_ACTION_RESULT")
        self.assertEqual(returns["MPFuryDPS"], "NO_ACTION_RESULT")
        self.assertFalse(returns["client_acceptance_inferred"])
        self.assertFalse(returns["server_outcome_inferred"])
        self.assertFalse(sinks["one_press_one_sink"])
        self.assertTrue(sinks["ordered_sink_trace_required"])
        self.assertFalse(sinks["press_count_is_action_count"])
        paths = {item["path_id"]: item for item in contract["reviewed_multi_sink_paths"]}
        self.assertEqual(
            paths["stance_then_charge"]["ordered_sinks"],
            ["CastSpellByName:Battle Stance", "CastSpellByName:Charge"],
        )
        self.assertEqual(
            paths["next_swing_then_primary_skill"]["ordered_sinks"],
            [
                "QueueSpellByName_OR_CastSpellByName:Heroic Strike/Cleave_OPTIONAL",
                "CastSpellByName_OR_QueueSpellByName:priority ability_OPTIONAL",
            ],
        )

    def test_validate_manifest_rejects_authority_promotion(self) -> None:
        manifest = deepcopy(load_manifest())
        manifest["source"]["authority"]["eligible_for_comparison"] = True
        with self.assertRaisesRegex(CatDeployedSourceManifestError, "eligible_for_comparison"):
            validate_manifest(manifest)

        manifest = deepcopy(load_manifest())
        manifest["comparison_readiness"]["exact_saved_profile_captured"] = True
        with self.assertRaisesRegex(CatDeployedSourceManifestError, "exact_saved_profile_captured"):
            validate_manifest(manifest)

    @unittest.skipUnless(
        EXTERNAL_DEPLOYED_CAT.is_dir(), "deployed Cat source tree is not available"
    )
    def test_live_deployed_tree_and_toc_closure_verify(self) -> None:
        facts = compute_source_tree_facts(EXTERNAL_DEPLOYED_CAT)
        self.assertEqual(facts["tree"], EXPECTED_TREE)
        self.assertEqual(facts["toc_closure"], EXPECTED_TOC_CLOSURE)
        self.assertEqual(facts["critical_files"], EXPECTED_CRITICAL_FILES)
        self.assertEqual(facts["unloaded_files"], ["Cat.toc", "\u4f7f\u7528\u6307\u5357.txt"])
        self.assertEqual(facts["unlisted_lua_or_xml"], [])
        self.assertIn(
            "MPWarriorFurySaved",
            facts["toc_metadata"]["saved_variables_per_character"],
        )
        receipt = verify_source_tree(EXTERNAL_DEPLOYED_CAT)
        self.assertEqual(receipt["status"], "PASS")
        self.assertFalse(receipt["client_load_observed"])
        self.assertFalse(receipt["policy_profile_included"])
        self.assertFalse(receipt["eligible_for_comparison"])

    @unittest.skipUnless(
        NESTED_REFERENCE_CAT.is_dir(), "nested Cat reference tree is not available"
    )
    def test_nested_reference_copy_is_not_the_deployed_identity(self) -> None:
        with self.assertRaisesRegex(CatDeployedSourceManifestError, "cat_root.tree"):
            verify_source_tree(NESTED_REFERENCE_CAT)

    @unittest.skipUnless(
        EXTERNAL_DEPLOYED_CAT.is_dir(), "deployed Cat source tree is not available"
    )
    def test_byte_and_member_drift_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            copied = Path(temporary_directory) / "Cat"
            shutil.copytree(EXTERNAL_DEPLOYED_CAT, copied)
            with (copied / "WarriorFury.lua").open("ab") as stream:
                stream.write(b"\n-- drift\n")
            with self.assertRaisesRegex(CatDeployedSourceManifestError, "cat_root.tree"):
                verify_source_tree(copied)

        with tempfile.TemporaryDirectory() as temporary_directory:
            copied = Path(temporary_directory) / "Cat"
            shutil.copytree(EXTERNAL_DEPLOYED_CAT, copied)
            (copied / "unexpected.lua").write_text("-- unexpected\n", encoding="utf-8")
            facts = compute_source_tree_facts(copied)
            self.assertEqual(facts["unlisted_lua_or_xml"], ["unexpected.lua"])
            with self.assertRaisesRegex(CatDeployedSourceManifestError, "cat_root.tree"):
                verify_source_tree(copied)


if __name__ == "__main__":
    unittest.main()
