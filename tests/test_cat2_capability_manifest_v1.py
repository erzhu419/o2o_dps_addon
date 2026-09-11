from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.cat2_capability_manifest_v1 import (
    DEFAULT_MANIFEST,
    EXPECTED_MANIFEST_SHA256,
    EXPECTED_PARTITIONS,
    Cat2CapabilityManifestError,
    compute_source_tree_facts,
    load_manifest,
    verify_source_tree,
)


EXTERNAL_CAT2_NEW = PROJECT_ROOT.parent / "Cat2_new"


def _minimal_cat2_tree(root: Path, *, duplicate_id: bool = False) -> Path:
    tree = root / "Cat2_new"
    warrior = tree / "Cards" / "Warrior"
    warrior.mkdir(parents=True)
    (tree / "Cat2.lua").write_text(
        'Cat2 = Cat2 or {}\nCat2.Version = "fixture"\n', encoding="utf-8"
    )
    first_id = "fixture_one"
    (warrior / "One.lua").write_text(
        "local card = {\n"
        f'  id = "{first_id}",\n'
        "  classes = {\n"
        "    WARRIOR = 2,\n"
        "  },\n"
        "}\n"
        "Cat2.RegisterCard(card)\n",
        encoding="utf-8",
    )
    toc = ["Cat2.lua", "Cards\\Warrior\\One.lua"]
    if duplicate_id:
        (warrior / "Two.lua").write_text(
            "local card = {\n"
            f'  id = "{first_id}",\n'
            "  classes = {\n"
            "    WARRIOR = 2,\n"
            "  },\n"
            "}\n"
            "Cat2.RegisterCard(card)\n",
            encoding="utf-8",
        )
        toc.append("Cards\\Warrior\\Two.lua")
    (tree / "Cat2.toc").write_text("\n".join(toc) + "\n", encoding="utf-8")
    return tree


class Cat2CapabilityManifestTests(unittest.TestCase):
    def test_manifest_is_exact_capability_source_not_policy_or_vote(self) -> None:
        document = load_manifest()

        self.assertEqual(document["source"]["archive"]["status"], "NOT_FOUND")
        self.assertIsNone(document["source"]["archive"]["sha256"])
        authority = document["source"]["authority"]
        self.assertEqual(authority["role"], "CAPABILITY_SOURCE")
        self.assertFalse(authority["deployed"])
        self.assertFalse(authority["source_execution"])
        self.assertFalse(authority["exact_runtime"])
        self.assertFalse(authority["eligible_for_independent_vote"])
        self.assertFalse(authority["policy_profile_included"])
        self.assertEqual(document["identity"]["tree"], EXPECTED_PARTITIONS["tree"])
        self.assertEqual(document["identity"]["cards"], EXPECTED_PARTITIONS["cards"])
        self.assertEqual(
            document["identity"]["warrior"], EXPECTED_PARTITIONS["warrior"]
        )

    def test_manifest_hash_and_third_party_source_boundary_are_pinned(self) -> None:
        digest = hashlib.sha256(DEFAULT_MANIFEST.read_bytes()).hexdigest()
        self.assertEqual(digest, EXPECTED_MANIFEST_SHA256)
        document = load_manifest()
        location = document["source"]["location"]
        self.assertEqual(location["kind"], "EXTERNAL_LOCAL_TREE")
        self.assertEqual(location["expected_directory_name"], "Cat2_new")
        self.assertFalse(location["embedded_third_party_lua"])
        self.assertFalse((PROJECT_ROOT / "Cat2_new").exists())
        self.assertEqual(
            list((PROJECT_ROOT / "configs" / "experts").rglob("*.lua")), []
        )

    def test_ordered_sink_and_traversal_semantics_are_explicit(self) -> None:
        contract = load_manifest()["execution_contract"]
        self.assertFalse(
            contract["return_semantics"]["client_acceptance_inferred"]
        )
        self.assertFalse(contract["return_semantics"]["server_outcome_inferred"])
        self.assertTrue(contract["sink_semantics"]["ordered_sink_trace_required"])
        self.assertEqual(len(contract["continuing_warrior_cards"]), 6)
        paths = {item["path_id"]: item for item in contract["multi_sink_paths"]}

        execute = paths["execute_interrupt_then_cast"]
        self.assertEqual(
            [item["sink"] for item in execute["ordered_sinks"]],
            ["SpellStopCasting", "Cat2.Cast"],
        )
        self.assertEqual(execute["traversal_after_path"], "STOP_ON_LITERAL_TRUE")

        nearby = paths["nearby_execute_loop"]
        self.assertEqual(
            [item["sink"] for item in nearby["ordered_sinks"]],
            [
                "SpellStopCasting",
                "TargetUnit",
                "CastSpellByName",
                "TargetUnit_OR_ClearTarget",
            ],
        )
        self.assertIn("UNSPECIFIED_LUA_PAIRS_ORDER", nearby["repeat"])
        self.assertEqual(
            nearby["traversal_after_path"], "CONTINUE_AFTER_LITERAL_FALSE"
        )

        stance = paths["auto_stance_multi_press"]
        self.assertIn("SEPARATE_HARDWARE_PRESSES", stance["repeat"])
        self.assertEqual(contract["zero_sink_true_example"]["action_sink_count"], 0)

    def test_new_identity_preserves_frozen_predecessor(self) -> None:
        lineage = load_manifest()["lineage"]
        self.assertEqual(lineage["relation"], "CANDIDATE_SUCCESSOR_OF")
        self.assertEqual(
            lineage["frozen_predecessor_expert_id"],
            "cat2.fury.brainofcat_shadow.saved_profile_source_v1",
        )
        self.assertFalse(lineage["replaces_predecessor"])
        self.assertFalse(lineage["reuse_predecessor_results"])
        self.assertTrue(lineage["migration_requires_new_profile_snapshot"])
        self.assertTrue(
            lineage["promotion_requires_deployed_tree_and_exact_sink_trace"]
        )

    def test_manifest_byte_drift_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            changed = Path(temporary_directory) / "manifest.json"
            document = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
            document["unexpected"] = True
            changed.write_text(
                json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                Cat2CapabilityManifestError, "content hash mismatch"
            ):
                load_manifest(changed)

    def test_generic_tree_inspector_detects_extra_lua_and_duplicate_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            tree = _minimal_cat2_tree(root)
            before = compute_source_tree_facts(tree)
            self.assertEqual(before["tree"]["file_count"], 3)
            self.assertEqual(before["cards"]["file_count"], 1)
            self.assertEqual(before["warrior"]["file_count"], 1)
            self.assertEqual(before["toc"]["unlisted_lua_count"], 0)

            extra = tree / "Cards" / "Warrior" / "Unlisted.lua"
            extra.write_text(
                "local card = {\n"
                '  id = "fixture_extra",\n'
                "  classes = {\n"
                "    WARRIOR = 2,\n"
                "  },\n"
                "}\n"
                "Cat2.RegisterCard(card)\n",
                encoding="utf-8",
            )
            after = compute_source_tree_facts(tree)
            self.assertNotEqual(before["tree"]["sha256"], after["tree"]["sha256"])
            self.assertEqual(after["tree"]["file_count"], 4)
            self.assertEqual(after["toc"]["unlisted_lua_count"], 1)

        with tempfile.TemporaryDirectory() as temporary_directory:
            duplicate = _minimal_cat2_tree(
                Path(temporary_directory), duplicate_id=True
            )
            with self.assertRaisesRegex(
                Cat2CapabilityManifestError, "duplicate card id"
            ):
                compute_source_tree_facts(duplicate)

    def test_source_inside_repository_boundary_fails_before_source_use(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            fake_repository = Path(temporary_directory) / "repo"
            embedded = fake_repository / "Cat2_new"
            embedded.mkdir(parents=True)
            with self.assertRaisesRegex(
                Cat2CapabilityManifestError, "must remain outside"
            ):
                verify_source_tree(
                    embedded,
                    project_root=fake_repository,
                )

    @unittest.skipUnless(
        EXTERNAL_CAT2_NEW.is_dir(), "external Cat2_new source is not available"
    )
    def test_live_external_cat2_new_matches_all_pins(self) -> None:
        receipt = verify_source_tree(EXTERNAL_CAT2_NEW)
        self.assertEqual(receipt["status"], "PASS")
        self.assertEqual(receipt["tree"], EXPECTED_PARTITIONS["tree"])
        self.assertEqual(receipt["cards"], EXPECTED_PARTITIONS["cards"])
        self.assertEqual(receipt["warrior"], EXPECTED_PARTITIONS["warrior"])
        self.assertFalse(receipt["deployed"])
        self.assertFalse(receipt["eligible_for_independent_vote"])

    @unittest.skipUnless(
        EXTERNAL_CAT2_NEW.is_dir(), "external Cat2_new source is not available"
    )
    def test_live_tree_byte_or_member_drift_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            copied = Path(temporary_directory) / "Cat2_new"
            shutil.copytree(EXTERNAL_CAT2_NEW, copied)
            with (copied / "Cat2.lua").open("ab") as stream:
                stream.write(b"\n-- drift\n")
            with self.assertRaisesRegex(
                Cat2CapabilityManifestError, "cat2_root.tree"
            ):
                verify_source_tree(copied)

        with tempfile.TemporaryDirectory() as temporary_directory:
            copied = Path(temporary_directory) / "Cat2_new"
            shutil.copytree(EXTERNAL_CAT2_NEW, copied)
            (copied / "unexpected-member.txt").write_text(
                "drift\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(
                Cat2CapabilityManifestError, "cat2_root.tree"
            ):
                verify_source_tree(copied)


if __name__ == "__main__":
    unittest.main()
