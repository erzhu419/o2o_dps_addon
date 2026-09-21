"""Read only compact v6 worker/manifest summaries to select one v7 TRAIN pilot."""

from __future__ import annotations

import json
from pathlib import Path
import shlex
import sys


BASE = "/home/zhengliang01/scheduleurm_work/o2o-dps-hpc"
V6_DISPATCH = (
    f"{BASE}/runs/teammate-response-current/single_scan_causal_target_choice_v6/"
    "7484e06074ef5a7a02a2bf6e06dcbca252777081040f75bfb2142616321c211a/"
    "attempt1/dispatch/dispatch.json"
)


def main() -> None:
    sys.path.insert(0, str(Path.home() / "mine_code/scheduleurm/skill"))
    import scheduler

    source = f"""
import gzip
import json
from pathlib import Path

dispatch = json.loads(Path({V6_DISPATCH!r}).read_text())
root = Path({BASE!r}) / dispatch['output_root_locator'] / 'workers'
by_id = {{task['instance_id']: task for task in dispatch['tasks']}}
ids = (
    '72878cca-a1e1-4698-8f44-24cf83491a73',
    '0b3d2ec3-12b5-4d0f-9d92-a75bec3010b4',
    '1d340245-6df6-4d86-895c-24a124682656',
    '9a61ace6-0292-4a97-bafa-76cead47cf38',
)
rows = []
for instance_id in ids:
    task = by_id[instance_id]
    with gzip.open(root / (instance_id + '.json.gz'), 'rt') as handle:
        worker = json.load(handle)
    base = worker['joint_dynamic_training_counts']['base_c']['tables']
    direct_6603 = sum(
        choice['count']
        for row in base['mark_counts'] if row['key'][0] == 'GLOBAL'
        for choice in row['counts']
        if json.loads(choice['value']) == ['DMG', 6603, 'DIRECT_FRIENDLY_PLAYER']
    )
    start_support = sum(
        choice['count']
        for row in base['target_choice_counts']
        if row['key'][0] == ['GLOBAL'] and row['key'][1] in ('FIRST_ACQUISITION', 'RETARGET')
        for choice in row['counts'] if choice['value'] is True
    )
    rows.append({{
        'instance_id': instance_id, 'node': task['node'],
        'compressed_bytes': task['partition']['compressed_size_bytes'],
        'wave_count': task['expected_summary']['wave_count'],
        'fury_episode_count': task['expected_summary']['fury_episode_count'],
        'direct_6603_dmg_count': direct_6603,
        'eligible_start_multitarget_choice_count': start_support,
    }})
print(json.dumps(rows))
"""
    command = (f"{BASE.rsplit('/o2o-dps-hpc', 1)[0]}/conda_envs/"
               f"scomp-py310/bin/python3.10 -c {shlex.quote(source)}")
    rc, stdout, stderr = scheduler.run_on("node003", command, timeout=60, check=False)
    if rc:
        raise RuntimeError(stderr[-1000:])
    print(stdout.strip())


if __name__ == "__main__":
    main()
