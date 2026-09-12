from __future__ import annotations

from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps import chronicle_external_encounter_reconstruction_v2 as reconstruction_v2
from o2o_dps import chronicle_external_reconstruction_admission_v1 as admission_v1
from o2o_dps import chronicle_fury46_target_identity_lookup_v1 as lookup


INSTANCE = "00000000-0000-0000-0000-000000000001"
ENCOUNTER = "00000000-0000-0000-0000-000000000002"
FURY_GUID = "0x00000000000000F1"
TARGET_A = "0xF13000F1ED000001"
TARGET_B = "0xF13000F1ED000002"
TARGET_MISSING = "0xF13000F1EE000003"
TARGET_PET = "0xF14001531500002D"


def _address(core: dict) -> dict:
    return lookup._content_addressed(core)


def _write_json(path: Path, value: dict) -> tuple[int, str]:
    payload = lookup._canonical(value) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return len(payload), hashlib.sha256(payload).hexdigest()


class Fury46TargetIdentityLookupV1Tests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, Path, Path]:
        raw_root = root / "raw"
        metadata = {
            "id": INSTANCE,
            "encounters": [
                {
                    "id": ENCOUNTER,
                    "hostiles": [{"id": TARGET_A, "boss": False}],
                }
            ],
            "units": {
                TARGET_A: {"name": "Greater Gloomwing", "entry": 61933},
                TARGET_B: {"name": "大型幽翼蝠", "entry": 61933},
                TARGET_PET: {
                    "name": "魔鳞魔网搜寻者",
                    "entry": 86805,
                    "owner": TARGET_A,
                    "controller": FURY_GUID,
                },
            },
        }
        metadata_path = raw_root / "objects" / "metadata.json"
        metadata_size, metadata_sha = _write_json(metadata_path, metadata)

        encounter = reconstruction_v2._content_addressed(
            {
                "encounter_id": ENCOUNTER,
                "waves": [
                    {
                        "wave_id": f"{ENCOUNTER}:external-v2-wave:0",
                        "targets": [
                            {
                                "target_guid": guid,
                                "voting_for_simulator_target_model": True,
                                "voting_status": lookup.VOTING_STATUS,
                            }
                            for guid in (
                                TARGET_A,
                                TARGET_B,
                                TARGET_MISSING,
                                TARGET_PET,
                            )
                        ],
                    }
                ],
            }
        )
        encounter_payload = lookup._canonical(encounter) + b"\n"
        compressed = gzip.compress(encounter_payload, mtime=0)
        encounter_path = root / "reconstruction" / "instances" / "encounter.json.gz"
        encounter_path.parent.mkdir(parents=True, exist_ok=True)
        encounter_path.write_bytes(compressed)
        encounter_sha = encounter["content_address"]["sha256"]

        admission = _address(
            {
                "schema": admission_v1.SCHEMA,
                "implementation_revision": admission_v1.IMPLEMENTATION_REVISION,
                "status": admission_v1.STATUS,
                "inputs": {"raw_api_manifest": {"file_sha256": "a" * 64}},
                "instances": [
                    {
                        "instance_id": INSTANCE,
                        "source_evidence": {
                            "metadata_object": {
                                "relative_path": "objects/metadata.json",
                                "sha256": metadata_sha,
                                "size_bytes": metadata_size,
                            }
                        },
                        "warrior_spec_evidence": {
                            "observations": [
                                {
                                    "player_class": "Warrior",
                                    "player_spec": "Fury",
                                    "spec_evidence_status": "OBSERVED",
                                    "field_conflicts": {},
                                    "player_guid": FURY_GUID,
                                },
                                {
                                    "player_class": "Warrior",
                                    "player_spec": "Arms",
                                    "spec_evidence_status": "OBSERVED",
                                    "field_conflicts": {},
                                    "player_guid": "0x00000000000000A1",
                                },
                            ]
                        },
                    }
                ],
            }
        )
        reconstruction = _address(
            {
                "schema": reconstruction_v2.SCHEMA,
                "implementation_revision": reconstruction_v2.IMPLEMENTATION_REVISION,
                "status": reconstruction_v2.STATUS,
                "instances": [
                    {
                        "instance_id": INSTANCE,
                        "encounters": [
                            {
                                "artifact": {
                                    "path": "instances/encounter.json.gz",
                                    "compressed_file_sha256": hashlib.sha256(
                                        compressed
                                    ).hexdigest(),
                                    "content_sha256": encounter_sha,
                                }
                            }
                        ],
                    }
                ],
            }
        )
        admission_path = root / "admission.json"
        reconstruction_path = root / "reconstruction" / "manifest.json"
        _write_json(admission_path, admission)
        _write_json(reconstruction_path, reconstruction)
        return admission_path, reconstruction_path, raw_root

    def test_exact_guid_entry_lookup_and_missing_stat_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            admission, reconstruction, raw_root = self._fixture(Path(temporary))
            value = lookup.build_target_identity_lookup_v1(
                admission_manifest_path=admission,
                reconstruction_manifest_path=reconstruction,
                raw_api_root=raw_root,
            )
            summary = lookup.validate_target_identity_lookup_v1(value)

        self.assertEqual(summary["selected_instance_count"], 1)
        self.assertEqual(summary["eligible_fury_observation_count"], 1)
        self.assertEqual(summary["unique_instance_target_guid_count"], 4)
        self.assertEqual(summary["chronicle_units_exact_guid_match_count"], 3)
        self.assertEqual(summary["positive_raw_chronicle_entry_count"], 3)
        self.assertEqual(summary["f130_creature_guid_count"], 3)
        self.assertEqual(summary["f140_pet_guid_count"], 1)
        self.assertEqual(summary["npc_entry_join_eligible_count"], 3)
        self.assertEqual(summary["npc_entry_join_resolved_count"], 2)
        self.assertEqual(summary["npc_entry_join_unresolved_count"], 2)
        self.assertEqual(summary["unique_npc_entry_count"], 1)
        self.assertEqual(summary["entry_with_multiple_observed_name_count"], 1)
        self.assertEqual(value["entry_name_variants"][0]["entry"], 61933)
        self.assertEqual(
            value["entry_name_variants"][0]["names"],
            ["Greater Gloomwing", "大型幽翼蝠"],
        )
        rows = {row["target_guid"]: row for row in value["target_lookups"]}
        self.assertEqual(rows[TARGET_A]["chronicle_unit"]["raw_entry"], 61933)
        self.assertEqual(rows[TARGET_A]["npc_table_join"]["entry"], 61933)
        self.assertEqual(rows[TARGET_PET]["chronicle_unit"]["raw_entry"], 86805)
        self.assertEqual(rows[TARGET_PET]["chronicle_unit"]["owner"], TARGET_A)
        self.assertEqual(rows[TARGET_PET]["chronicle_unit"]["controller"], FURY_GUID)
        self.assertFalse(rows[TARGET_PET]["npc_table_join"]["eligible"])
        self.assertIsNone(rows[TARGET_PET]["npc_table_join"]["entry"])
        self.assertEqual(rows[TARGET_A]["encounter_boss_observation_values"], [False])
        self.assertEqual(
            rows[TARGET_MISSING]["chronicle_unit"]["identity_status"],
            "MISSING_FROM_CHRONICLE_UNITS_MAP",
        )
        self.assertTrue(value["claim_boundary"]["identity_lookup_usable"])
        self.assertFalse(value["claim_boundary"]["dynamic_target_stats_complete"])
        for field in lookup.MISSING_STAT_FIELDS:
            self.assertIsNone(rows[TARGET_A]["dynamic_target_stats"][field]["value"])

    def test_metadata_binding_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            admission, reconstruction, raw_root = self._fixture(Path(temporary))
            metadata_path = raw_root / "objects" / "metadata.json"
            metadata_path.write_bytes(metadata_path.read_bytes() + b" ")
            with self.assertRaisesRegex(
                lookup.Fury46TargetIdentityLookupError,
                "metadata differs from its admission binding",
            ):
                lookup.build_target_identity_lookup_v1(
                    admission_manifest_path=admission,
                    reconstruction_manifest_path=reconstruction,
                    raw_api_root=raw_root,
                )

    def test_validator_rejects_promoted_target_stat(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            admission, reconstruction, raw_root = self._fixture(Path(temporary))
            value = lookup.build_target_identity_lookup_v1(
                admission_manifest_path=admission,
                reconstruction_manifest_path=reconstruction,
                raw_api_root=raw_root,
            )
        core = deepcopy(value)
        core.pop("content_address")
        core["target_lookups"][0]["dynamic_target_stats"]["base_armor"]["value"] = 4091
        mutated = _address(core)
        with self.assertRaisesRegex(
            lookup.Fury46TargetIdentityLookupError,
            "promotes a missing dynamic target stat",
        ):
            lookup.validate_target_identity_lookup_v1(mutated)

    def test_validator_rejects_f140_raw_entry_as_npc_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            admission, reconstruction, raw_root = self._fixture(Path(temporary))
            value = lookup.build_target_identity_lookup_v1(
                admission_manifest_path=admission,
                reconstruction_manifest_path=reconstruction,
                raw_api_root=raw_root,
            )
        core = deepcopy(value)
        core.pop("content_address")
        pet_row = next(
            row for row in core["target_lookups"] if row["target_guid"] == TARGET_PET
        )
        pet_row["npc_table_join"]["entry"] = pet_row["chronicle_unit"]["raw_entry"]
        mutated = _address(core)
        with self.assertRaisesRegex(
            lookup.Fury46TargetIdentityLookupError,
            "non-F130 target promotes",
        ):
            lookup.validate_target_identity_lookup_v1(mutated)

    def test_write_is_deterministic_and_addressed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            admission, reconstruction, raw_root = self._fixture(root)
            value = lookup.build_target_identity_lookup_v1(
                admission_manifest_path=admission,
                reconstruction_manifest_path=reconstruction,
                raw_api_root=raw_root,
            )
            first = lookup.write_target_identity_lookup_v1(value, root / "out")
            second = lookup.write_target_identity_lookup_v1(value, root / "out")
            self.assertEqual(first, second)
            with gzip.open(first["path"], "rt", encoding="utf-8") as handle:
                loaded = json.load(handle)
            self.assertEqual(
                loaded["content_address"]["sha256"], first["content_sha256"]
            )


if __name__ == "__main__":
    unittest.main()
