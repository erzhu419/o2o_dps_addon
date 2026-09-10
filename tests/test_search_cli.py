from __future__ import annotations

import json
from contextlib import redirect_stdout
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.beam_search import SearchResult
from o2o_dps.search_cli import DEFAULT_FURY_SPELL_IDS, main


class _Bridge:
    def __init__(self, executable: Path) -> None:
        self.executable = executable

    def __enter__(self) -> "_Bridge":
        return self

    def __exit__(self, *args: object) -> None:
        return None


class SearchCLITests(unittest.TestCase):
    def test_writes_explicit_uncalibrated_teacher_document(self) -> None:
        result = SearchResult(
            best_action_sequence=(),
            score=42.0,
            frontier_state={"damage_done": 42.0},
            teacher_states=(),
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            request = root / "request.json"
            output = root / "teacher.json"
            request.write_text('{"raid": {}, "encounter": {}}', encoding="utf-8")
            with patch("o2o_dps.search_cli.SimulatorBridge", _Bridge), patch(
                "o2o_dps.search_cli.beam_search", return_value=result
            ) as search:
                with redirect_stdout(io.StringIO()):
                    return_code = main(
                        [
                            str(request),
                            "--bridge",
                            str(root / "bridge.exe"),
                            "--output",
                            str(output),
                        ]
                    )

            self.assertEqual(return_code, 0)
            document = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(document["result"]["score"], 42.0)
            self.assertIn("uncalibrated", document["validation_status"])
            self.assertEqual(document["spell_id_allowlist"], list(DEFAULT_FURY_SPELL_IDS))
            self.assertEqual(search.call_args.kwargs["spell_id_allowlist"], DEFAULT_FURY_SPELL_IDS)


if __name__ == "__main__":
    unittest.main()
