"""List recent post-fix Upper Kara Fury candidate raids without event downloads."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from o2o_dps.chronicle_external_api_ingest_v1 import ChronicleClient
from o2o_dps.chronicle_postfix_warrior_candidate_manifest_v1 import (
    build_postfix_warrior_candidate_manifest_v1,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upload-after", required=True, help="RFC3339 upload cursor")
    parser.add_argument("--page-size", type=int, default=50)
    parser.add_argument("--max-pages", type=int, default=4)
    parser.add_argument("--output", type=Path, help="Small JSON manifest; stdout if omitted")
    args = parser.parse_args(argv)
    manifest = build_postfix_warrior_candidate_manifest_v1(
        ChronicleClient(),
        upload_after=args.upload_after,
        page_size=args.page_size,
        max_pages=args.max_pages,
    )
    body = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    if args.output is None:
        sys.stdout.write(body)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(body, encoding="utf-8")
        print(f"{manifest['raid_count']} raids; {manifest['ranked_fury_raid_count']} ranked Fury raids: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
