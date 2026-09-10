"""Extract Chronicle leaderboard rows and instance links from printed PDFs.

The PDFs are local selection evidence.  This module does not contact Chronicle;
it uses the ``pdftotext`` and ``pdfinfo`` executables already installed on the
Windows host to pair each visible leaderboard row with its embedded instance
link.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import html
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable, Iterable, Sequence
import unicodedata
from urllib.parse import parse_qs, unquote, urlparse
import xml.etree.ElementTree as ET


SCHEMA_VERSION = 1
MANIFEST_KIND = "chronicle_leaderboard_pdf_manifest"
SITE_HOST = "capy.chronicleclassic.com"
INSTANCE_URL_PREFIX = f"https://{SITE_HOST}/instances/"

MEDAL_RANKS = {"🥇": 1, "🥈": 2, "🥉": 3}
PDFINFO_URL = re.compile(r"^\s*(?P<page>\d+)\s+\S+\s+(?P<url>https?://\S+)\s*$")
LEADERBOARD_URL = re.compile(
    rf"https://{re.escape(SITE_HOST)}/leaderboards\?[^\s]+"
)
CAPTURED_AT = re.compile(r"(?m)^\s*(\d{4}/\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2})\s+")
PLAYER_TOTAL = re.compile(
    r"(?m)^\s*(?P<players>\d+)\s+players\b.*?"
    r"Page\s+(?P<page>\d+)\s+of\s+(?P<pages>\d+)\s*$"
)

ToolRunner = Callable[[Sequence[str]], str]


class ChronicleBoardPDFError(ValueError):
    """A supplied PDF cannot be mapped to exact Chronicle leaderboard rows."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _run_tool(command: Sequence[str]) -> str:
    try:
        completed = subprocess.run(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError as error:
        raise ChronicleBoardPDFError(
            f"required PDF tool is not installed or not on PATH: {command[0]}"
        ) from error
    except OSError as error:
        raise ChronicleBoardPDFError(
            f"failed to start PDF tool {command[0]}: {error}"
        ) from error

    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        suffix = f": {detail}" if detail else ""
        raise ChronicleBoardPDFError(
            f"{command[0]} exited with code {completed.returncode}{suffix}"
        )
    try:
        return completed.stdout.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ChronicleBoardPDFError(
            f"{command[0]} did not return UTF-8 output"
        ) from error


def _instance_slug(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != SITE_HOST:
        return None
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) != 2 or parts[0] != "instances" or not parts[1]:
        return None
    return parts[1]


def parse_pdfinfo_instance_urls(output: str) -> dict[int, list[str]]:
    """Return embedded Chronicle instance URLs in annotation order, by page."""

    by_page: dict[int, list[str]] = {}
    for line in output.splitlines():
        match = PDFINFO_URL.match(line)
        if match is None:
            continue
        url = match.group("url")
        if _instance_slug(url) is None:
            continue
        by_page.setdefault(int(match.group("page")), []).append(url)
    return by_page


def _row_rank(raw_rank: str) -> int:
    if raw_rank in MEDAL_RANKS:
        return MEDAL_RANKS[raw_rank]
    return int(raw_rank)


def _xml_root(output: str, tool: str) -> ET.Element:
    try:
        return ET.fromstring(output)
    except ET.ParseError as error:
        raise ChronicleBoardPDFError(f"{tool} returned invalid XML: {error}") from error


def _normalized_element_text(element: ET.Element) -> str:
    return unicodedata.normalize("NFKC", "".join(element.itertext())).strip()


def _linked_tokens(page: ET.Element) -> list[dict[str, Any]]:
    tokens: list[dict[str, Any]] = []
    for element in page.findall("text"):
        anchor = next(element.iter("a"), None)
        if anchor is None:
            continue
        href = str(anchor.get("href") or "")
        if _instance_slug(href) is None:
            continue
        tokens.append(
            {
                "top": int(element.get("top", "0")),
                "left": int(element.get("left", "0")),
                "width": int(element.get("width", "0")),
                "height": int(element.get("height", "0")),
                "text": _normalized_element_text(element),
                "href": href,
            }
        )
    return tokens


def _bbox_pages(bbox_root: ET.Element) -> dict[int, dict[str, Any]]:
    pages: dict[int, dict[str, Any]] = {}
    for page_number, page in enumerate(bbox_root.findall(".//{*}page"), start=1):
        words = []
        for word in page.findall(".//{*}word"):
            text = "".join(word.itertext()).strip()
            if not text:
                continue
            words.append(
                {
                    "x_min": float(word.get("xMin", "0")),
                    "y_min": float(word.get("yMin", "0")),
                    "x_max": float(word.get("xMax", "0")),
                    "y_max": float(word.get("yMax", "0")),
                    "text": text,
                }
            )
        pages[page_number] = {
            "width": float(page.get("width", "0")),
            "height": float(page.get("height", "0")),
            "words": words,
        }
    return pages


def _character_from_bbox(
    character_token: dict[str, Any],
    *,
    html_width: float,
    html_height: float,
    bbox_page: dict[str, Any],
) -> str:
    scale_x = float(bbox_page["width"]) / html_width
    scale_y = float(bbox_page["height"]) / html_height
    target_x = (character_token["left"] + character_token["width"] / 2) * scale_x
    target_y = (character_token["top"] + character_token["height"] / 2) * scale_y

    candidates = []
    for word in bbox_page["words"]:
        center_x = (word["x_min"] + word["x_max"]) / 2
        center_y = (word["y_min"] + word["y_max"]) / 2
        if abs(center_y - target_y) > 8:
            continue
        distance = abs(center_x - target_x) + 3 * abs(center_y - target_y)
        candidates.append((distance, word))
    if not candidates:
        raise ChronicleBoardPDFError("could not recover character text from bbox XML")
    word = min(candidates, key=lambda item: item[0])[1]
    character = str(word["text"]).strip()
    if not character or character in {"Statistics", "Speedruns"}:
        raise ChronicleBoardPDFError("bbox XML selected a page overlay instead of a character")
    return character


def parse_geometry_rows(html_xml: str, bbox_xml: str) -> list[dict[str, Any]]:
    """Parse rows by linked geometry, using bbox text as character-name truth."""

    html_root = _xml_root(html_xml, "pdftohtml")
    bbox_by_page = _bbox_pages(_xml_root(bbox_xml, "pdftotext -bbox-layout"))
    rows: list[dict[str, Any]] = []

    for page in html_root.findall("page"):
        page_number = int(page.get("number", "0"))
        html_width = float(page.get("width", "0"))
        html_height = float(page.get("height", "0"))
        bbox_page = bbox_by_page.get(page_number)
        if html_width <= 0 or html_height <= 0 or bbox_page is None:
            raise ChronicleBoardPDFError(f"missing page geometry for PDF page {page_number}")

        tokens = _linked_tokens(page)
        rank_tokens = [
            token
            for token in tokens
            if token["left"] / html_width < 0.18
            and (token["text"] in MEDAL_RANKS or token["text"].isdigit())
        ]
        rank_tokens.sort(key=lambda token: (token["top"], token["left"]))

        for position, rank_token in enumerate(rank_tokens):
            previous_top = rank_tokens[position - 1]["top"] if position else None
            next_top = (
                rank_tokens[position + 1]["top"]
                if position + 1 < len(rank_tokens)
                else None
            )
            lower = (
                (previous_top + rank_token["top"]) / 2
                if previous_top is not None
                else rank_token["top"] - 40
            )
            upper = (
                (rank_token["top"] + next_top) / 2
                if next_top is not None
                else rank_token["top"] + 55
            )
            row_tokens = [
                token
                for token in tokens
                if token["href"] == rank_token["href"]
                and lower <= token["top"] < upper
            ]

            header_tokens = [
                token
                for token in row_tokens
                if abs(token["top"] - rank_token["top"]) <= 10
            ]
            character_candidates = [
                token
                for token in header_tokens
                if token is not rank_token and token["left"] / html_width < 0.55
            ]
            if not character_candidates:
                raise ChronicleBoardPDFError(
                    f"page {page_number} rank {rank_token['text']}: character anchor missing"
                )
            character_token = min(
                character_candidates,
                key=lambda token: abs(token["left"] / html_width - 0.217),
            )

            dps_candidates = [
                token
                for token in header_tokens
                if token["left"] / html_width > 0.70
                and re.fullmatch(r"\d[\d,]*(?:\.\d+)?", token["text"])
            ]
            if len(dps_candidates) != 1:
                raise ChronicleBoardPDFError(
                    f"page {page_number} rank {rank_token['text']}: expected one DPS value"
                )

            detail_tokens = [
                token
                for token in row_tokens
                if 12 <= token["top"] - rank_token["top"] <= 42
            ]
            realm_tokens = [
                token for token in detail_tokens if token["text"] == "Basin of Stars"
            ]
            if len(realm_tokens) != 1:
                raise ChronicleBoardPDFError(
                    f"page {page_number} rank {rank_token['text']}: realm is ambiguous"
                )
            spec_candidates = [
                token
                for token in detail_tokens
                if token["left"] / html_width < 0.23
                and token["text"] != "Basin of Stars"
            ]
            if not spec_candidates:
                raise ChronicleBoardPDFError(
                    f"page {page_number} rank {rank_token['text']}: spec label missing"
                )
            spec_token = min(
                spec_candidates,
                key=lambda token: abs(token["left"] / html_width - 0.183),
            )

            date_numbers = [
                int(token["text"])
                for token in sorted(detail_tokens, key=lambda token: token["left"])
                if token["left"] / html_width > 0.70 and token["text"].isdigit()
            ]
            if len(date_numbers) != 3:
                raise ChronicleBoardPDFError(
                    f"page {page_number} rank {rank_token['text']}: date is ambiguous"
                )
            year, month, day = date_numbers

            dps_text = dps_candidates[0]["text"].replace(",", "")
            dps: int | float = float(dps_text) if "." in dps_text else int(dps_text)
            rows.append(
                {
                    "rank": _row_rank(rank_token["text"]),
                    "character": _character_from_bbox(
                        character_token,
                        html_width=html_width,
                        html_height=html_height,
                        bbox_page=bbox_page,
                    ),
                    "observed_spec": spec_token["text"],
                    "realm": realm_tokens[0]["text"],
                    "dps": dps,
                    "raid_date": f"{year:04d}-{month:02d}-{day:02d}",
                    "source_page": page_number,
                    "instance_slug": _instance_slug(rank_token["href"]),
                    "instance_url": rank_token["href"],
                }
            )
    return rows


def _leaderboard_metadata(layout_text: str, source: Path) -> dict[str, Any]:
    urls = []
    for raw_url in LEADERBOARD_URL.findall(layout_text):
        url = html.unescape(raw_url)
        if url not in urls:
            urls.append(url)
    if len(urls) != 1:
        raise ChronicleBoardPDFError(
            f"{source.name}: expected one leaderboard source URL, found {len(urls)}"
        )

    source_url = urls[0]
    query = parse_qs(urlparse(source_url).query)
    raid = str((query.get("instance") or [""])[0]).strip()
    board_class = str((query.get("class") or [""])[0]).strip()
    if not raid or not board_class:
        raise ChronicleBoardPDFError(
            f"{source.name}: leaderboard URL lacks instance or class"
        )
    raw_spec = str((query.get("spec") or [""])[0]).strip()

    captured_match = CAPTURED_AT.search(layout_text)
    total_match = PLAYER_TOTAL.search(layout_text)
    metadata: dict[str, Any] = {
        "source_pdf": str(source),
        "source_file": source.name,
        "source_url": source_url,
        "raid": raid,
        "board_class": board_class,
        "board_spec": raw_spec or None,
        "period": str((query.get("period") or ["all"])[0]),
    }
    if captured_match is not None:
        metadata["captured_at_display"] = captured_match.group(1)
    if total_match is not None:
        metadata["reported_players"] = int(total_match.group("players"))
        metadata["reported_leaderboard_page"] = int(total_match.group("page"))
        metadata["reported_leaderboard_pages"] = int(total_match.group("pages"))
    return metadata


def parse_board_pdf(
    source_pdf: str | Path,
    *,
    run_tool: ToolRunner = _run_tool,
) -> dict[str, Any]:
    """Extract and strictly pair one leaderboard PDF's rows and hyperlinks."""

    source = Path(source_pdf).expanduser().resolve()
    if not source.is_file():
        raise ChronicleBoardPDFError(f"leaderboard PDF does not exist: {source}")

    layout_text = run_tool(
        ("pdftotext", "-layout", "-enc", "UTF-8", str(source), "-")
    )
    bbox_xml = run_tool(
        ("pdftotext", "-bbox-layout", "-enc", "UTF-8", str(source), "-")
    )
    html_xml = run_tool(
        ("pdftohtml", "-xml", "-hidden", "-i", "-stdout", str(source))
    )
    url_text = run_tool(("pdfinfo", "-url", str(source)))
    metadata = _leaderboard_metadata(layout_text, source)
    rows = parse_geometry_rows(html_xml, bbox_xml)
    urls_by_page = parse_pdfinfo_instance_urls(url_text)

    if not rows:
        raise ChronicleBoardPDFError(f"{source.name}: no leaderboard rows were found")
    ranks = [row["rank"] for row in rows]
    expected_ranks = list(range(1, len(rows) + 1))
    if ranks != expected_ranks:
        raise ChronicleBoardPDFError(
            f"{source.name}: extracted ranks are not continuous 1..{len(rows)}: {ranks}"
        )

    rows_by_page: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        rows_by_page.setdefault(int(row["source_page"]), []).append(row)

    for page_number in sorted(set(rows_by_page) | set(urls_by_page)):
        page_rows = rows_by_page.get(page_number, [])
        page_urls = urls_by_page.get(page_number, [])
        row_urls = [str(row["instance_url"]) for row in page_rows]
        if Counter(row_urls) != Counter(page_urls):
            raise ChronicleBoardPDFError(
                f"{source.name} page {page_number}: linked row URLs do not match "
                "the PDF annotations"
            )
        for row in page_rows:
            row["source_file"] = source.name
            row["board_class"] = metadata["board_class"]
            row["board_spec"] = metadata["board_spec"]

    metadata["extracted_row_count"] = len(rows)
    metadata["physical_page_count"] = len(_xml_root(html_xml, "pdftohtml").findall("page"))
    metadata["rows"] = rows
    return metadata


def build_manifest(pdf_paths: Iterable[str | Path]) -> dict[str, Any]:
    sources = sorted(
        {Path(path).expanduser().resolve() for path in pdf_paths},
        key=lambda path: path.name.casefold(),
    )
    if not sources:
        raise ChronicleBoardPDFError("no leaderboard PDFs were supplied")

    boards = [parse_board_pdf(path) for path in sources]
    raids = {str(board["raid"]) for board in boards}
    if len(raids) != 1:
        raise ChronicleBoardPDFError(
            "supplied PDFs cover more than one raid: " + ", ".join(sorted(raids))
        )

    entries = [row for board in boards for row in board["rows"]]
    unique_instances = {str(row["instance_slug"]) for row in entries}
    unique_characters = {
        (str(row["realm"]), str(row["character"])) for row in entries
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": MANIFEST_KIND,
        "generated_at": _utc_now(),
        "selection_policy": "all_rows_in_supplied_pdf_prints",
        "server": "Capybara",
        "raid": next(iter(raids)),
        "summary": {
            "pdf_count": len(boards),
            "leaderboard_row_count": len(entries),
            "unique_character_count": len(unique_characters),
            "unique_instance_count": len(unique_instances),
        },
        "sources": [
            {key: value for key, value in board.items() if key != "rows"}
            for board in boards
        ],
        "entries": entries,
    }


def save_manifest(manifest: dict[str, Any], output: str | Path) -> Path:
    destination = Path(output).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        temporary.replace(destination)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise ChronicleBoardPDFError(
            f"cannot write leaderboard manifest {destination}: {error}"
        ) from error
    return destination


def load_manifest(source: str | Path) -> dict[str, Any]:
    path = Path(source).expanduser().resolve()
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ChronicleBoardPDFError(f"cannot read leaderboard manifest {path}: {error}") from error
    if not isinstance(manifest, dict) or manifest.get("kind") != MANIFEST_KIND:
        raise ChronicleBoardPDFError(f"not a Chronicle leaderboard manifest: {path}")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ChronicleBoardPDFError(
            f"unsupported leaderboard manifest schema: {manifest.get('schema_version')!r}"
        )
    if not isinstance(manifest.get("entries"), list):
        raise ChronicleBoardPDFError(f"leaderboard manifest has no entries list: {path}")
    return manifest


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chronicle_board_manifest",
        description="Build a local Chronicle leaderboard manifest from printed PDFs.",
    )
    parser.add_argument("--pdf-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    pdf_directory = args.pdf_dir.expanduser().resolve()
    if not pdf_directory.is_dir():
        print(f"Chronicle PDF manifest failed: directory does not exist: {pdf_directory}", file=sys.stderr)
        return 2
    pdfs = sorted(pdf_directory.glob("*.pdf"), key=lambda path: path.name.casefold())
    try:
        manifest = build_manifest(pdfs)
        path = save_manifest(manifest, args.output)
    except ChronicleBoardPDFError as error:
        print(f"Chronicle PDF manifest failed: {error}", file=sys.stderr)
        return 2

    summary = manifest["summary"]
    print(
        f"已提取 {summary['pdf_count']} 份 PDF、"
        f"{summary['leaderboard_row_count']} 条排行榜记录、"
        f"{summary['unique_instance_count']} 个去重实例。"
    )
    print(f"Manifest：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
