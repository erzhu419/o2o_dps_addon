"""Stream the one verified jump-host Chronicle raw capture to node004.

Run this from WSL. The tar stream passes through local memory, never a local
file. Source and destination are the dedicated 4183 external-check directories.
"""

from __future__ import annotations

from pathlib import Path
import shlex
import subprocess
import sys


SCHEDULER_SKILL = Path("~/mine_code/scheduleurm/skill").expanduser()
sys.path.insert(0, str(SCHEDULER_SKILL))
import scheduler  # type: ignore[import-not-found]  # noqa: E402


SOURCE = Path(
    "/home/erzhu419/.local/share/o2o-dps-hpc/"
    "external-white6603-v8-4183-20260921"
)
DESTINATION = Path(
    "/home/zhengliang01/scheduleurm_work/o2o-dps-hpc/runs/"
    "teammate-response-current/development_white6603_v8_external_4183_20260921"
)


def main() -> int:
    target = scheduler._ssh_target_for_node("node004")
    ssh_to_node = shlex.split(scheduler._ssh_rsync_shell_for_node("node004"))
    source_command = [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
        "jtl110gpu2", f"tar -C {shlex.quote(str(SOURCE))} -cf - offline_data",
    ]
    destination_command = [
        *ssh_to_node, target,
        f"mkdir -p {shlex.quote(str(DESTINATION))} && "
        f"tar -C {shlex.quote(str(DESTINATION))} -xf -",
    ]
    source = subprocess.Popen(source_command, stdout=subprocess.PIPE)
    assert source.stdout is not None
    try:
        destination = subprocess.Popen(
            destination_command, stdin=source.stdout,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        source.stdout.close()
        output, errors = destination.communicate(timeout=180)
        source_status = source.wait(timeout=10)
    except BaseException:
        source.kill()
        source.wait()
        raise
    if source_status or destination.returncode:
        print(
            f"raw stream failed: source={source_status}, "
            f"destination={destination.returncode}, stderr={errors.decode(errors='replace')[:1000]}",
            file=sys.stderr,
        )
        return 2
    print("STREAMED_SINGLE_RAID_RAW_TO_NODE004")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
