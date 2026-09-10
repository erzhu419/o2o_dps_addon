from __future__ import annotations

from contextlib import redirect_stdout
import copy
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.cat2_saved_profile_v1 import (
    AUTHORITY_STATE,
    Cat2SavedProfileError,
    EXPECTED_CARD_ORDER,
    PINNED_BRAINOF_CAT_SOURCE_SHA256,
    PINNED_SOURCE_SHA256,
    load_cat2_saved_profile,
    main,
)


def _lua_value(value: object, *, indent: int = 0) -> str:
    prefix = "\t" * indent
    child = "\t" * (indent + 1)
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "nil"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if type(value) in {int, float}:
        return repr(value)
    if isinstance(value, list):
        body = "".join(
            f"\n{child}[{index}] = {_lua_value(item, indent=indent + 1)},"
            for index, item in enumerate(value, start=1)
        )
        return "{" + body + f"\n{prefix}" + "}"
    if isinstance(value, dict):
        body = "".join(
            f"\n{child}[{json.dumps(str(key))}] = {_lua_value(item, indent=indent + 1)},"
            for key, item in value.items()
        )
        return "{" + body + f"\n{prefix}" + "}"
    raise TypeError(value)


def _profile_document() -> dict[str, object]:
    option_values = {
        "warrior_o2o_policy_brain": {"liveMode": False},
        "warrior_bloodrage": {"maximumRage": 30},
        "warrior_heroic_strike_alt": {"rageThreshold": 50},
    }
    steps = [
        {
            "id": card_id,
            "enabled": 1,
            "minimizedVisible": 1,
            "optionValues": option_values.get(card_id, {}),
        }
        for card_id in EXPECTED_CARD_ORDER
    ]
    return {
        "schemaVersion": 1,
        "configurations": {
            "schemaVersion": 1,
            "activeProfileId": 1,
            "nextProfileId": 2,
            "profileOrder": [1],
            "profiles": [
                {"id": 1, "name": "BrainOfCat Shadow", "steps": steps}
            ],
        },
        "ui": {"shortcutWindows": [{"visible": 0, "scale": 1}]},
    }


class Cat2SavedProfileTests(unittest.TestCase):
    def _fixture(
        self, root: Path
    ) -> tuple[Path, Path, Path, dict[str, str], dict[str, str]]:
        installed = root / "Cat2"
        pins: dict[str, str] = {}
        for index, relative in enumerate(PINNED_SOURCE_SHA256, start=1):
            path = installed / Path(relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = f"-- deterministic fixture {index}: {relative}\n".encode()
            path.write_bytes(payload)
            pins[relative] = hashlib.sha256(payload).hexdigest()
        brainofcat = root / "BrainOfCat"
        brain_pins: dict[str, str] = {}
        for index, relative in enumerate(
            PINNED_BRAINOF_CAT_SOURCE_SHA256, start=1
        ):
            path = brainofcat / Path(relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = f"-- deterministic brain fixture {index}: {relative}\n".encode()
            path.write_bytes(payload)
            brain_pins[relative] = hashlib.sha256(payload).hexdigest()
        savedvariables = root / "Cat2.lua"
        return savedvariables, installed, brainofcat, pins, brain_pins

    def _write(self, path: Path, document: dict[str, object], whitespace: str = "\n") -> None:
        path.write_text(
            f"-- literal fixture{whitespace}Cat2CharacterDB = {_lua_value(document)}{whitespace}",
            encoding="utf-8",
        )

    def _load(
        self,
        savedvariables: Path,
        installed: Path,
        brainofcat: Path,
        pins: dict[str, str],
        brain_pins: dict[str, str],
    ) -> dict[str, object]:
        with patch.dict(PINNED_SOURCE_SHA256, pins, clear=True), patch.dict(
            PINNED_BRAINOF_CAT_SOURCE_SHA256, brain_pins, clear=True
        ):
            return load_cat2_saved_profile(
                savedvariables, "BrainOfCat Shadow", installed, brainofcat
            )

    def test_loads_strict_active_shadow_profile_and_pins_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            savedvariables, installed, brainofcat, pins, brain_pins = self._fixture(root)
            self._write(savedvariables, _profile_document())
            result = self._load(savedvariables, installed, brainofcat, pins, brain_pins)

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["authority_state"], AUTHORITY_STATE)
            self.assertFalse(result["execution_authorized"])
            self.assertFalse(result["deployment_allowed"])
            self.assertEqual(result["selection"]["active_profile_id"], 1)
            self.assertEqual(
                [step["id"] for step in result["profile"]["steps"]],
                list(EXPECTED_CARD_ORDER),
            )
            self.assertFalse(
                result["profile"]["steps"][0]["option_values"]["liveMode"]
            )
            self.assertEqual(result["source_bundle"]["pin_status"], "PINNED_EXACT")
            self.assertEqual(
                result["source_bundle"]["file_count"], len(pins) + len(brain_pins)
            )
            self.assertEqual(
                result["source_bundle"]["scope"],
                "DIRECT_RUNTIME_DEPENDENCY_PINNED",
            )
            self.assertFalse(
                result["source_bundle"]["transitive_dependency_closure_claimed"]
            )
            self.assertEqual(len(result["profile_semantic_sha256"]), 64)
            self.assertEqual(len(result["raw_savedvariables_sha256"]), 64)
            self.assertIsInstance(result["savedvariables"]["mtime_ns"], int)

    def test_source_pins_are_lowercase_sha256(self) -> None:
        self.assertEqual(len(PINNED_SOURCE_SHA256), 15)
        self.assertEqual(len(PINNED_BRAINOF_CAT_SOURCE_SHA256), 1)
        for root_kind, pins in (
            ("cat2", PINNED_SOURCE_SHA256),
            ("brainofcat", PINNED_BRAINOF_CAT_SOURCE_SHA256),
        ):
            for relative, digest in pins.items():
                with self.subTest(root_kind=root_kind, relative=relative):
                    self.assertRegex(digest, re.compile(r"\A[0-9a-f]{64}\Z"))

    def test_semantic_hash_ignores_ui_minimized_visibility_and_whitespace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            savedvariables, installed, brainofcat, pins, brain_pins = self._fixture(root)
            document = _profile_document()
            self._write(savedvariables, document)
            first = self._load(savedvariables, installed, brainofcat, pins, brain_pins)

            changed = copy.deepcopy(document)
            changed["ui"] = {"anything": "different", "nested": [1, 2, 3]}
            changed["configurations"]["profiles"][0]["steps"][3][
                "minimizedVisible"
            ] = 0
            self._write(savedvariables, changed, whitespace="\n\n\t")
            second = self._load(savedvariables, installed, brainofcat, pins, brain_pins)

            self.assertEqual(
                first["profile_semantic_sha256"], second["profile_semantic_sha256"]
            )
            self.assertNotEqual(
                first["raw_savedvariables_sha256"], second["raw_savedvariables_sha256"]
            )

    def test_semantic_hash_changes_for_behavioral_option(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            savedvariables, installed, brainofcat, pins, brain_pins = self._fixture(root)
            document = _profile_document()
            self._write(savedvariables, document)
            first = self._load(savedvariables, installed, brainofcat, pins, brain_pins)
            document["configurations"]["profiles"][0]["steps"][3]["optionValues"][
                "maximumRage"
            ] = 31
            self._write(savedvariables, document)
            second = self._load(savedvariables, installed, brainofcat, pins, brain_pins)
            self.assertNotEqual(
                first["profile_semantic_sha256"], second["profile_semantic_sha256"]
            )

    def test_live_unknown_reordered_and_bad_types_fail_closed(self) -> None:
        mutations = []
        live = _profile_document()
        live["configurations"]["profiles"][0]["steps"][0]["optionValues"][
            "liveMode"
        ] = True
        mutations.append(live)
        unknown = _profile_document()
        unknown["configurations"]["profiles"][0]["steps"][7]["id"] = "unknown_card"
        mutations.append(unknown)
        reordered = _profile_document()
        steps = reordered["configurations"]["profiles"][0]["steps"]
        steps[1], steps[2] = steps[2], steps[1]
        mutations.append(reordered)
        bad_enabled = _profile_document()
        bad_enabled["configurations"]["profiles"][0]["steps"][1]["enabled"] = True
        mutations.append(bad_enabled)
        sparse = _profile_document()
        sparse["configurations"]["profileOrder"] = {"1": 1, "3": 1}
        mutations.append(sparse)

        for index, document in enumerate(mutations):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                savedvariables, installed, brainofcat, pins, brain_pins = self._fixture(root)
                self._write(savedvariables, document)
                with self.assertRaises(Cat2SavedProfileError):
                    self._load(savedvariables, installed, brainofcat, pins, brain_pins)

    def test_rejects_extra_assignment_and_malicious_expression_without_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            savedvariables, installed, brainofcat, pins, brain_pins = self._fixture(root)
            marker = root / "must_not_exist"
            savedvariables.write_text(
                "Cat2CharacterDB = (function() "
                f"local f=io.open({json.dumps(str(marker))}, 'w'); f:write('bad'); "
                "f:close(); return {} end)()\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(Cat2SavedProfileError, "literal-only"):
                self._load(savedvariables, installed, brainofcat, pins, brain_pins)
            self.assertFalse(marker.exists())

            self._write(savedvariables, _profile_document())
            savedvariables.write_text(
                savedvariables.read_text(encoding="utf-8") + "OtherDB = {}\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(Cat2SavedProfileError, "only permitted"):
                self._load(savedvariables, installed, brainofcat, pins, brain_pins)

    def test_source_hash_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            savedvariables, installed, brainofcat, pins, brain_pins = self._fixture(root)
            self._write(savedvariables, _profile_document())
            first_relative = next(iter(pins))
            (installed / Path(first_relative)).write_text("-- changed\n", encoding="utf-8")
            with self.assertRaisesRegex(Cat2SavedProfileError, "source hash mismatch"):
                self._load(savedvariables, installed, brainofcat, pins, brain_pins)

    def test_cli_atomically_writes_strict_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            savedvariables, installed, brainofcat, pins, brain_pins = self._fixture(root)
            self._write(savedvariables, _profile_document())
            output = root / "nested" / "snapshot.json"
            stdout = io.StringIO()
            with patch.dict(PINNED_SOURCE_SHA256, pins, clear=True), patch.dict(
                PINNED_BRAINOF_CAT_SOURCE_SHA256, brain_pins, clear=True
            ), redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--savedvariables",
                        str(savedvariables),
                        "--profile-name",
                        "BrainOfCat Shadow",
                        "--installed-root",
                        str(installed),
                        "--brainofcat-root",
                        str(brainofcat),
                        "--output",
                        str(output),
                    ]
                )
            self.assertEqual(exit_code, 0)
            written = json.loads(output.read_text(encoding="utf-8"))
            receipt = json.loads(stdout.getvalue())
            self.assertEqual(written["status"], "ok")
            self.assertEqual(receipt["output"], str(output.resolve()))
            self.assertFalse(any(output.parent.glob(f".{output.name}.*.tmp")))

    def test_cli_refuses_to_overwrite_savedvariables_or_pinned_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            savedvariables, installed, brainofcat, pins, brain_pins = self._fixture(root)
            self._write(savedvariables, _profile_document())
            original_savedvariables = savedvariables.read_bytes()
            protected_source = installed / Path(next(iter(pins)))
            original_source = protected_source.read_bytes()
            protected_brain_source = brainofcat / Path(next(iter(brain_pins)))
            original_brain_source = protected_brain_source.read_bytes()

            for output in (savedvariables, protected_source, protected_brain_source):
                with self.subTest(output=output), patch.dict(
                    PINNED_SOURCE_SHA256, pins, clear=True
                ), patch.dict(
                    PINNED_BRAINOF_CAT_SOURCE_SHA256, brain_pins, clear=True
                ), self.assertRaisesRegex(Cat2SavedProfileError, "refusing to overwrite"):
                    main(
                        [
                            "--savedvariables",
                            str(savedvariables),
                            "--profile-name",
                            "BrainOfCat Shadow",
                            "--installed-root",
                            str(installed),
                            "--brainofcat-root",
                            str(brainofcat),
                            "--output",
                            str(output),
                        ]
                    )

            self.assertEqual(savedvariables.read_bytes(), original_savedvariables)
            self.assertEqual(protected_source.read_bytes(), original_source)
            self.assertEqual(protected_brain_source.read_bytes(), original_brain_source)


if __name__ == "__main__":
    unittest.main()
