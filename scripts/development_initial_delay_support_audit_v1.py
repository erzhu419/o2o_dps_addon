"""Read-only aggregate of WAVE_START delay support in one formal runtime store."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3


def audit(path: Path) -> dict:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        manifest = json.loads(connection.execute(
            "SELECT value_json FROM metadata WHERE key = 'manifest'"
        ).fetchone()[0])
        support = Counter()
        distribution_count = Counter()
        class_support = Counter()
        for raw_key, count in connection.execute(
            "SELECT key_json, support FROM distribution_lookup WHERE head = 'delay_counts'"
        ):
            key = json.loads(raw_key)
            if len(key) < 2 or key[1] != "WAVE_START":
                continue
            level = key[0]
            alive = "NOT_IN_KEY" if level == "GLOBAL" else key[5 if level == "CLASS_SPEC" else 4]
            support[(level, alive)] += count
            distribution_count[(level, alive)] += 1
            if level == "CLASS":
                class_support[(key[2], alive)] += count
        return {
            "store_path": str(path),
            "revision": manifest["revision"],
            "model_content_sha256": manifest["model_content_sha256"],
            "variant_id": manifest["variant_id"],
            "wave_start_support_by_level_alive": [
                {"level": level, "alive_bucket": alive, "support": count,
                 "distribution_count": distribution_count[(level, alive)]}
                for (level, alive), count in sorted(support.items())
            ],
            "wave_start_class_support": [
                {"class": hero_class, "alive_bucket": alive, "support": count}
                for (hero_class, alive), count in sorted(class_support.items())
            ],
        }
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("store", type=Path)
    args = parser.parse_args()
    print(json.dumps(audit(args.store), ensure_ascii=False))


if __name__ == "__main__":
    main()
