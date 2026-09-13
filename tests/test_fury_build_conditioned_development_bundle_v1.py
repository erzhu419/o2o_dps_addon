from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from o2o_dps import historical_fury_decision_build_join_v1 as join_v1
from o2o_dps.fury_build_conditioned_development_bundle_v1 import (
    BLOODTHIRST_REPRESENTATIVE_SELECTION_POLICY,
    DEVELOPMENT_BLOODTHIRST_REPRESENTATIVE_LIMIT,
    DEVELOPMENT_SEED_COUNT,
    EXPECTED_CONTRA_RUNTIME_BINDING_SHA256,
    EXPECTED_RUNTIME_SNAPSHOT_SHA256,
    FuryBuildConditionedDevelopmentBundleV1Error,
    PROJECT_ROOT,
    _address,
    _select_development_bloodthirst_ranks,
    build_fury_build_conditioned_development_bundle_v1,
    publish_fury_build_conditioned_development_bundle_v1,
    validate_fury_build_conditioned_development_bundle_v1,
)


CURRENT_SELECTED_BLOODTHIRST_RANKS = (2, 7, 9, 11, 14)


def _readdress(bundle: dict[str, object]) -> dict[str, object]:
    addressed = deepcopy(bundle)
    addressed.pop("bundle_sha256", None)
    payload = json.dumps(
        addressed,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    addressed["bundle_sha256"] = hashlib.sha256(payload).hexdigest()
    return addressed


class FuryBuildConditionedDevelopmentBundleV1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = build_fury_build_conditioned_development_bundle_v1()

    def test_current_bytes_select_first_five_when_more_exact_builds_are_covered(self) -> None:
        bundle = self.bundle
        self.assertEqual(bundle["status"], "PREPARED")
        self.assertEqual(bundle["execution_status"], "NOT_RUN")
        self.assertFalse(bundle["comparison_authorized"])
        self.assertFalse(bundle["deployment_authorized"])
        self.assertFalse(bundle["scientific_runs_started"])
        self.assertFalse(bundle["frozen_v3_mutated"])
        self.assertEqual(bundle["blockers"], [])
        self.assertEqual(
            bundle["runtime_inputs"]["runtime_snapshot"]["snapshot_sha256"],
            EXPECTED_RUNTIME_SNAPSHOT_SHA256,
        )
        self.assertEqual(
            bundle["runtime_inputs"]["contra_runtime_binding"]["binding_sha256"],
            EXPECTED_CONTRA_RUNTIME_BINDING_SHA256,
        )
        canonical_binding = bundle["runtime_inputs"][
            "contra_runtime_binding_canonical_bytes"
        ]
        self.assertEqual(
            canonical_binding["binding_sha256"],
            EXPECTED_CONTRA_RUNTIME_BINDING_SHA256,
        )

        requests = bundle["requests"]
        self.assertEqual(
            [request["representative_rank"] for request in requests],
            list(CURRENT_SELECTED_BLOODTHIRST_RANKS),
        )
        self.assertEqual(len(requests), 5)
        self.assertEqual(
            {request["weapon_mode"] for request in requests},
            {"TWO_HAND", "DUAL_WIELD"},
        )
        for request in requests:
            self.assertTrue(request["runtime_executable"])
            self.assertFalse(request["comparison_eligible"])
            components = request["five_part_components"]
            self.assertEqual(
                set(components),
                {
                    "CharacterProfile",
                    "RaidContext",
                    "EncounterModel",
                    "ExecutionModel",
                    "Objective",
                },
            )
            selection = components["CharacterProfile"]["selection"]
            self.assertEqual(
                selection["selection_mode"], "historical_exact_representative"
            )
            self.assertNotEqual(
                selection["record"]["event"], "STATIC_PROFILE_CAPTURED"
            )
            self.assertTrue(
                any(
                    talent.get("name") == "bloodthirst" and talent.get("rank") == 1
                    for talent in selection["record"]["state"]["talents"]
                )
            )
            self.assertEqual(
                request["composition"]["audit"]["admission"]["status"],
                "ADMITTED",
            )
            self.assertIsNotNone(request["composition"]["request"])

        p0 = bundle["p0_inputs"]
        self.assertEqual(
            set(p0),
            {
                "talent_map",
                "coverage_registry",
                "build_catalog_manifest",
                "build_catalog_data",
                "representative_selector_manifest",
                "representatives",
            },
        )
        for identity in p0.values():
            self.assertGreater(identity["size_bytes"], 0)
            self.assertEqual(len(identity["byte_sha256"]), 64)
            self.assertTrue(Path(identity["path"]).is_file())
        self.assertEqual(
            p0["representatives"]["bloodthirst_ranks"],
            [
                2,
                7,
                9,
                11,
                14,
                15,
                17,
                18,
                22,
                26,
                27,
                30,
                31,
                35,
                36,
                38,
                39,
                42,
                51,
                53,
                54,
                57,
                60,
            ],
        )
        self.assertEqual(
            p0["representatives"]["selected_bloodthirst_ranks"],
            list(CURRENT_SELECTED_BLOODTHIRST_RANKS),
        )
        self.assertEqual(
            p0["representatives"]["selection_policy"],
            BLOODTHIRST_REPRESENTATIVE_SELECTION_POLICY,
        )
        self.assertEqual(
            p0["representatives"]["selection_limit"],
            DEVELOPMENT_BLOODTHIRST_REPRESENTATIVE_LIMIT,
        )
        self.assertEqual(
            bundle["request_contract"]["representative_selection_policy"],
            BLOODTHIRST_REPRESENTATIVE_SELECTION_POLICY,
        )
        self.assertEqual(
            bundle["request_contract"]["representative_selection_limit"],
            DEVELOPMENT_BLOODTHIRST_REPRESENTATIVE_LIMIT,
        )
        validate_fury_build_conditioned_development_bundle_v1(bundle)

    def test_selection_uses_selector_order_and_blocks_when_fewer_than_five(self) -> None:
        observed = (20, 19, 16, 15, 11, 10, 8, 7, 4, 1)
        self.assertEqual(
            _select_development_bloodthirst_ranks(observed),
            observed[:DEVELOPMENT_BLOODTHIRST_REPRESENTATIVE_LIMIT],
        )
        with self.assertRaisesRegex(
            FuryBuildConditionedDevelopmentBundleV1Error,
            "fewer exact Bloodthirst representatives are available",
        ):
            _select_development_bloodthirst_ranks((1, 4, 7, 8))

    def test_seed_and_requested_lane_closure_are_declared_but_empty(self) -> None:
        seeds = self.bundle["development_seeds"]
        self.assertEqual(seeds["count"], DEVELOPMENT_SEED_COUNT)
        self.assertEqual(len(seeds["master_seeds"]), DEVELOPMENT_SEED_COUNT)
        self.assertEqual(len(set(seeds["master_seeds"])), DEVELOPMENT_SEED_COUNT)
        self.assertTrue(seeds["fixed_sample_no_optional_stopping"])
        self.assertTrue(seeds["development_only"])

        closure = self.bundle["requested_lane_closure"]
        self.assertEqual(closure["status"], "REQUESTED_NOT_RUN")
        self.assertEqual(
            [lane["lane_family"] for lane in closure["lanes"]],
            [
                "CAT",
                "CONTRA_DEPLOYED",
                "CONTRA_NEW",
                "HISTORICAL_EXPERT",
                "CANDIDATE",
            ],
        )
        self.assertEqual(
            closure["requested_cell_count"],
            DEVELOPMENT_BLOODTHIRST_REPRESENTATIVE_LIMIT * 32 * 5,
        )
        self.assertEqual(closure["completed_receipt_count"], 0)
        self.assertEqual(closure["admitted_lane_count"], 0)
        self.assertTrue(all(not lane["admitted"] for lane in closure["lanes"]))
        historical = next(
            lane
            for lane in closure["lanes"]
            if lane["lane_family"] == "HISTORICAL_EXPERT"
        )
        self.assertEqual(
            historical["required_closure"],
            "EXECUTABLE_SOURCE_BOUND_PROTOTYPE_AND_FULL_POLICY_RECEIPTS_REQUIRED",
        )

    def test_wrong_runtime_snapshot_returns_precise_not_run_blocker(self) -> None:
        source_path = Path(
            self.bundle["runtime_inputs"]["runtime_snapshot"]["path"]
        )
        changed = json.loads(source_path.read_text(encoding="utf-8"))
        changed["snapshot_sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as temporary:
            changed_path = Path(temporary) / "wrong-snapshot.json"
            changed_path.write_text(
                json.dumps(changed, ensure_ascii=False), encoding="utf-8"
            )
            blocked = build_fury_build_conditioned_development_bundle_v1(
                runtime_snapshot_path=changed_path
            )

        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertEqual(blocked["execution_status"], "NOT_RUN")
        self.assertFalse(blocked["comparison_authorized"])
        self.assertFalse(blocked["deployment_authorized"])
        self.assertFalse(blocked["scientific_runs_started"])
        self.assertEqual(blocked["requests"], [])
        self.assertEqual(
            blocked["blockers"][0]["code"],
            "RUNTIME_SNAPSHOT_IDENTITY_MISMATCH",
        )
        validate_fury_build_conditioned_development_bundle_v1(
            blocked, verify_input_bytes=False
        )

    def test_bundle_address_detects_post_preparation_mutation(self) -> None:
        changed = deepcopy(self.bundle)
        changed["comparison_authorized"] = True
        with self.assertRaisesRegex(
            FuryBuildConditionedDevelopmentBundleV1Error,
            "bundle_sha256 mismatch",
        ):
            validate_fury_build_conditioned_development_bundle_v1(
                changed, verify_input_bytes=False
            )

    def test_bundle_identity_and_byte_validation_survive_project_relocation(self) -> None:
        bundle = deepcopy(self.bundle)

        def absolute_strings(value: object) -> list[str]:
            if isinstance(value, dict):
                return [
                    found
                    for item in value.values()
                    for found in absolute_strings(item)
                ]
            if isinstance(value, list):
                return [
                    found
                    for item in value
                    for found in absolute_strings(item)
                ]
            if isinstance(value, str):
                normalized = value.replace("\\", "/")
                if normalized.startswith("/") or (
                    len(normalized) >= 3
                    and normalized[1] == ":"
                    and normalized[2] == "/"
                ):
                    return [value]
            return []

        self.assertEqual(absolute_strings(bundle), [])
        runtime_identity = bundle["runtime_inputs"]["runtime_snapshot"]
        identities = [runtime_identity, *bundle["p0_inputs"].values()]
        for identity in identities:
            self.assertFalse(Path(identity["path"]).is_absolute())

        alternate = deepcopy(bundle)
        alternate.pop("bundle_sha256")
        portable_runtime_path = runtime_identity["path"].replace("/", "\\")
        alternate["runtime_inputs"]["runtime_snapshot"]["path"] = (
            r"Z:\relocated\o2o-dps" + "\\" + portable_runtime_path
        )
        self.assertEqual(
            _address(alternate)["bundle_sha256"],
            bundle["bundle_sha256"],
        )

        with tempfile.TemporaryDirectory(prefix="bundle_relocation_") as temporary:
            relocated_root = Path(temporary) / "project-copy"
            for identity in identities:
                relative = Path(identity["path"])
                source = PROJECT_ROOT / relative
                destination = relocated_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)

            checked = validate_fury_build_conditioned_development_bundle_v1(
                bundle,
                verify_input_bytes=True,
                input_root=relocated_root,
            )
            self.assertEqual(checked["bundle_sha256"], bundle["bundle_sha256"])

            bundle_directory = (
                relocated_root
                / "offline_data"
                / "derived"
                / "fury_build_conditioned_development_bundle"
                / "v1"
            )
            bundle_directory.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                bundle,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            ).encode("utf-8") + b"\n"
            current = bundle_directory / "current.json"
            addressed = bundle_directory / (
                "fury_build_conditioned_development_bundle_v1."
                f"{bundle['bundle_sha256']}.json"
            )
            current.write_bytes(payload)
            addressed.write_bytes(payload)
            routes, binding = join_v1._load_current_requests(
                current,
                input_root=relocated_root,
            )
            self.assertEqual(len(routes), DEVELOPMENT_BLOODTHIRST_REPRESENTATIVE_LIMIT)
            self.assertEqual(binding["status"], "PREPARED")
            self.assertTrue(
                binding["source_bytes_and_declared_bundle_sha256_verified"]
            )

            relocated_runtime = relocated_root / Path(runtime_identity["path"])
            relocated_runtime.write_bytes(relocated_runtime.read_bytes() + b"\n")
            with self.assertRaisesRegex(
                FuryBuildConditionedDevelopmentBundleV1Error,
                "bound input bytes changed",
            ):
                validate_fury_build_conditioned_development_bundle_v1(
                    bundle,
                    verify_input_bytes=True,
                    input_root=relocated_root,
                )

    def test_readdressing_cannot_bypass_semantic_closure(self) -> None:
        cases = []

        changed = deepcopy(self.bundle)
        changed["deployment_authorized"] = True
        cases.append(("deployment authority", changed))

        changed = deepcopy(self.bundle)
        changed["runtime_inputs"]["contra_runtime_binding"][
            "authority_boundary"
        ]["comparison_ready"] = True
        cases.append(("nested Contra comparison authority", changed))

        changed = deepcopy(self.bundle)
        changed["claim_boundary"] = "SUPERIORITY_ESTABLISHED"
        cases.append(("claim boundary", changed))

        changed = deepcopy(self.bundle)
        changed["development_seeds"]["master_seeds"] = changed[
            "development_seeds"
        ]["master_seeds"][:1]
        cases.append(("truncated seed list", changed))

        changed = deepcopy(self.bundle)
        changed["requested_lane_closure"]["lanes"][0]["admitted"] = True
        cases.append(("admitted CAT lane", changed))

        changed = deepcopy(self.bundle)
        changed["requested_lane_closure"]["requested_cell_count"] = 1
        cases.append(("incorrect cell count", changed))

        changed = deepcopy(self.bundle)
        changed["requests"][0]["five_part_components"]["Objective"][
            "kind"
        ] = "FORGED_OBJECTIVE"
        cases.append(("component/composition divergence", changed))

        changed = deepcopy(self.bundle)
        changed["p0_inputs"]["representatives"][
            "selected_bloodthirst_ranks"
        ] = [1, 4, 7, 8, 11]
        cases.append(("stale selected representative ranks", changed))

        for label, changed in cases:
            with self.subTest(label=label), self.assertRaises(
                FuryBuildConditionedDevelopmentBundleV1Error
            ):
                validate_fury_build_conditioned_development_bundle_v1(
                    _readdress(changed), verify_input_bytes=False
                )

    def test_atomic_publication_writes_identical_stable_and_addressed_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            receipt = publish_fury_build_conditioned_development_bundle_v1(
                output_directory=temporary,
                bundle=self.bundle,
            )
            addressed = Path(receipt["content_addressed_path"])
            current = Path(receipt["current_path"])
            addressed_bytes = addressed.read_bytes()
            self.assertEqual(addressed_bytes, current.read_bytes())
            self.assertEqual(
                addressed.name,
                "fury_build_conditioned_development_bundle_v1."
                f"{self.bundle['bundle_sha256']}.json",
            )
            persisted = json.loads(addressed_bytes.decode("utf-8"))
            self.assertEqual(persisted["status"], "PREPARED")
            self.assertEqual(persisted["execution_status"], "NOT_RUN")
            self.assertFalse(persisted["comparison_authorized"])
            self.assertEqual(receipt["bundle_sha256"], self.bundle["bundle_sha256"])


if __name__ == "__main__":
    unittest.main()
