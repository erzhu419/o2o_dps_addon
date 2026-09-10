from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.chronicle_board_manifest import (
    ChronicleBoardPDFError,
    parse_board_pdf,
    parse_geometry_rows,
    parse_pdfinfo_instance_urls,
)


FIRST_URL = "https://capy.chronicleclassic.com/instances/first-slug_"
SECOND_URL = "https://capy.chronicleclassic.com/instances/second-slug-"


HTML_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<pdf2xml>
  <page number="1" width="900" height="1200">
    <text top="100" left="123" width="25" height="23"><a href="{FIRST_URL}">🥇</a></text>
    <text top="97" left="194" width="72" height="23"><a href="{FIRST_URL}"><b>仙⻛道⻣</b></a></text>
    <text top="100" left="727" width="47" height="19"><a href="{FIRST_URL}"><b>1,181</b></a></text>
    <text top="125" left="163" width="34" height="20"><a href="{FIRST_URL}">Arms</a></text>
    <text top="125" left="206" width="88" height="20"><a href="{FIRST_URL}">Basin of Stars</a></text>
    <text top="126" left="684" width="31" height="18"><a href="{FIRST_URL}">2026</a></text>
    <text top="126" left="715" width="12" height="18"><a href="{FIRST_URL}">年</a></text>
    <text top="126" left="727" width="8" height="18"><a href="{FIRST_URL}">8</a></text>
    <text top="126" left="735" width="12" height="18"><a href="{FIRST_URL}">月</a></text>
    <text top="126" left="747" width="15" height="18"><a href="{FIRST_URL}">16</a></text>
    <text top="126" left="762" width="12" height="18"><a href="{FIRST_URL}">日</a></text>
    <text top="106" left="150" width="90" height="18"><a href="{FIRST_URL}">Statistics</a></text>
    <text top="106" left="287" width="80" height="18"><a href="{FIRST_URL}">Speedruns</a></text>

    <text top="180" left="130" width="10" height="23"><a href="{SECOND_URL}"><b>2</b></a></text>
    <text top="177" left="194" width="65" height="23"><a href="{SECOND_URL}"><b>Narcissly</b></a></text>
    <text top="180" left="727" width="47" height="19"><a href="{SECOND_URL}"><b>990</b></a></text>
    <text top="205" left="163" width="34" height="20"><a href="{SECOND_URL}">Fury</a></text>
    <text top="205" left="206" width="88" height="20"><a href="{SECOND_URL}">Basin of Stars</a></text>
    <text top="206" left="684" width="31" height="18"><a href="{SECOND_URL}">2026</a></text>
    <text top="206" left="715" width="12" height="18"><a href="{SECOND_URL}">年</a></text>
    <text top="206" left="727" width="8" height="18"><a href="{SECOND_URL}">8</a></text>
    <text top="206" left="735" width="12" height="18"><a href="{SECOND_URL}">月</a></text>
    <text top="206" left="747" width="15" height="18"><a href="{SECOND_URL}">26</a></text>
    <text top="206" left="762" width="12" height="18"><a href="{SECOND_URL}">日</a></text>
  </page>
</pdf2xml>
"""


BBOX_XML = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
  <body><doc><page width="600" height="800">
    <word xMin="130" yMin="65" xMax="168" yMax="80">仙风道骨</word>
    <word xMin="130" yMin="118" xMax="173" yMax="133">Narcissly</word>
    <word xMin="100" yMin="69" xMax="155" yMax="82">Statistics</word>
  </page></doc></body>
</html>
"""


LAYOUT_TEXT = """2026/8/28 13:18        Capybara by Chronicle
Upper Tower of Karazhan
https://capy.chronicleclassic.com/leaderboards?instance=Upper+Tower+of+Karazhan&tab=leaderboard&class=WARRIOR&spec=Arms
2 players                                             Page 1 of 1
"""


PDFINFO_TEXT = f"""Page  Type          URL
   1  Annotation    {FIRST_URL}
   1  Annotation    {SECOND_URL}
   1  Annotation    https://capy.chronicleclassic.com/
"""


class ChronicleBoardManifestTests(unittest.TestCase):
    def test_geometry_keeps_true_name_when_sticky_overlay_is_linked(self) -> None:
        rows = parse_geometry_rows(HTML_XML, BBOX_XML)

        self.assertEqual([row["rank"] for row in rows], [1, 2])
        self.assertEqual(rows[0]["character"], "仙风道骨")
        self.assertEqual(rows[0]["instance_slug"], "first-slug_")
        self.assertEqual(rows[0]["dps"], 1181)
        self.assertEqual(rows[0]["observed_spec"], "Arms")
        self.assertEqual(rows[0]["raid_date"], "2026-08-16")
        self.assertEqual(rows[1]["character"], "Narcissly")
        self.assertEqual(rows[1]["dps"], 990)

    def test_pdfinfo_keeps_slug_trailing_punctuation(self) -> None:
        urls = parse_pdfinfo_instance_urls(PDFINFO_TEXT)
        self.assertEqual(urls, {1: [FIRST_URL, SECOND_URL]})

    def test_parse_board_pairs_geometry_with_annotation_multiset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "board.pdf"
            source.write_bytes(b"fixture marker")

            def fake_tool(command: list[str] | tuple[str, ...]) -> str:
                if command[0] == "pdftohtml":
                    return HTML_XML
                if command[0] == "pdfinfo":
                    return PDFINFO_TEXT
                if "-bbox-layout" in command:
                    return BBOX_XML
                return LAYOUT_TEXT

            board = parse_board_pdf(source, run_tool=fake_tool)

        self.assertEqual(board["raid"], "Upper Tower of Karazhan")
        self.assertEqual(board["board_class"], "WARRIOR")
        self.assertEqual(board["board_spec"], "Arms")
        self.assertEqual(board["period"], "all")
        self.assertEqual(board["reported_players"], 2)
        self.assertEqual(board["extracted_row_count"], 2)
        self.assertEqual(board["rows"][0]["source_file"], "board.pdf")

    def test_annotation_mismatch_fails_instead_of_guessing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "board.pdf"
            source.write_bytes(b"fixture marker")

            def fake_tool(command: list[str] | tuple[str, ...]) -> str:
                if command[0] == "pdftohtml":
                    return HTML_XML
                if command[0] == "pdfinfo":
                    return f"Page Type URL\n1 Annotation {FIRST_URL}\n"
                if "-bbox-layout" in command:
                    return BBOX_XML
                return LAYOUT_TEXT

            with self.assertRaisesRegex(
                ChronicleBoardPDFError, "linked row URLs do not match"
            ):
                parse_board_pdf(source, run_tool=fake_tool)


if __name__ == "__main__":
    unittest.main()
