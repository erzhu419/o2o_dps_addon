"""Exact Windows/WSL trace check for the same ``o2obridge`` source checkout.

This diagnostic is intentionally narrow.  It verifies that two bridge
executables return byte-identical canonical JSON for one fixed request, seed,
and deterministic sequence of GCD actions.  A pass is evidence about this
transport/build pair only; it is not simulator validation, policy evaluation,
or evidence of scientific equivalence between operating systems.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .expert_proposals import ACTION_KEY_TO_REF
from .sim_bridge import AvailableAction, SimulatorBridge


JSONMap = dict[str, Any]
SCHEMA = "fury_bridge_platform_equivalence/v1"
EVIDENCE_SCOPE = "FIXED_REQUEST_SEED_TRACE_ONLY"
ACTION_RULE = "epoch0_slam_if_legal_then_lexicographic_first_legal_gcd"

_RESULT_BEARING_ACTION_REFS = frozenset(
    ACTION_KEY_TO_REF[action_key]
    for action_key in (
        "warrior.bloodthirst",
        "warrior.whirlwind",
        "warrior.slam",
        "warrior.execute",
        "warrior.hamstring",
        "warrior.pummel",
        "warrior.sunder_armor",
    )
)


class FuryBridgePlatformEquivalenceError(RuntimeError):
    """A launch, input, trace, or comparison precondition failed."""


@dataclass(frozen=True)
class FileIdentity:
    path: str
    size_bytes: int
    sha256: str


def canonical_bytes(value: Any) -> bytes:
    """Return the only byte representation used for trace comparison."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise FuryBridgePlatformEquivalenceError(
            f"trace is not finite canonical JSON: {exc}"
        ) from exc


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def snapshot_file(path: Path) -> tuple[FileIdentity, bytes]:
    resolved = path.expanduser().resolve()
    try:
        data = resolved.read_bytes()
    except OSError as exc:
        raise FuryBridgePlatformEquivalenceError(
            f"could not read {resolved}: {exc}"
        ) from exc
    return (
        FileIdentity(
            path=str(resolved),
            size_bytes=len(data),
            sha256=sha256_bytes(data),
        ),
        data,
    )


def load_request(path: Path) -> tuple[FileIdentity, JSONMap]:
    identity, data = snapshot_file(path)
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FuryBridgePlatformEquivalenceError(
            f"request is not valid JSON: {identity.path}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise FuryBridgePlatformEquivalenceError("request JSON must be an object")
    return identity, value


def windows_path_to_wsl(path: Path) -> str:
    """Convert one resolved drive-letter Windows path to a WSL mount path."""

    resolved = path.expanduser().resolve()
    drive = resolved.drive
    if len(drive) != 2 or drive[1] != ":" or not drive[0].isalpha():
        raise FuryBridgePlatformEquivalenceError(
            f"WSL launch requires a drive-letter path, got {resolved}"
        )
    posix = resolved.as_posix()
    suffix = posix[3:] if len(posix) > 3 else ""
    return f"/mnt/{drive[0].lower()}/{suffix}"


def linux_wsl_arguments(
    *, distro: str, simulator_root: Path, linux_bridge: Path
) -> tuple[str, ...]:
    if not isinstance(distro, str) or not distro.strip():
        raise FuryBridgePlatformEquivalenceError("WSL distro must be nonempty")
    return (
        "-d",
        distro,
        "--cd",
        windows_path_to_wsl(simulator_root),
        "--",
        windows_path_to_wsl(linux_bridge),
    )


def action_record(action: AvailableAction) -> JSONMap:
    return asdict(action)


def _action_sort_key(action: AvailableAction) -> tuple[int, int, int, int, int]:
    return (
        action.action.spell_id,
        action.action.item_id,
        action.action.other_id,
        action.action.tag,
        action.index,
    )


def capture_trace(
    bridge: SimulatorBridge,
    request: Mapping[str, Any],
    *,
    seed: int,
    decision_count: int,
) -> list[JSONMap]:
    """Capture a deterministic, outcome-bearing bridge trace.

    At the first decision point Turtle Slam is chosen when legal so the trace
    exercises a result that remains pending across the immediate acceptance
    boundary.  Later decisions use the lexicographically first legal GCD
    action.  Both platforms therefore consume the same requested action stream;
    the complete action inventory, apply result, pending/result partition, and
    post-event state remain in the compared trace.
    """

    if isinstance(seed, bool) or not isinstance(seed, int):
        raise TypeError("seed must be an integer")
    if (
        isinstance(decision_count, bool)
        or not isinstance(decision_count, int)
        or decision_count <= 0
    ):
        raise ValueError("decision_count must be a positive integer")

    trace: list[JSONMap] = [
        {"command": "load", "state": bridge.load(request, seed)}
    ]
    outstanding_attempt_ids: list[str] = []
    for epoch in range(decision_count):
        actions = bridge.actions()
        trace.append(
            {
                "command": "actions",
                "epoch": epoch,
                "actions": [action_record(action) for action in actions],
            }
        )
        legal = [
            action for action in actions if action.legal and action.triggers_gcd
        ]
        if not legal:
            raise FuryBridgePlatformEquivalenceError(
                f"no legal GCD action at decision epoch {epoch}"
            )
        slam = ACTION_KEY_TO_REF["warrior.slam"]
        selected = next(
            (
                action
                for action in legal
                if epoch == 0 and action.action == slam
            ),
            min(legal, key=_action_sort_key),
        )
        attempt_id = (
            f"platform-equivalence:{epoch}"
            if selected.action in _RESULT_BEARING_ACTION_REFS
            else None
        )
        applied = bridge.act(selected.action, attempt_id=attempt_id)
        if attempt_id is not None and applied.casted:
            outstanding_attempt_ids.append(attempt_id)
        trace.append(
            {
                "command": "act",
                "epoch": epoch,
                "selected": asdict(selected.action),
                "attempt_id": attempt_id,
                "result": asdict(applied),
            }
        )
        result_batch = bridge.server_results_since_last_decision(
            tuple(outstanding_attempt_ids)
        )
        trace.append(
            {
                "command": "server_results",
                "phase": "post_act",
                "epoch": epoch,
                "result": asdict(result_batch),
            }
        )
        outstanding_attempt_ids = list(result_batch.pending_attempt_ids)
        if applied.finished:
            break
        advanced = bridge.advance()
        trace.append(
            {"command": "advance", "epoch": epoch, "state": advanced}
        )
        result_batch = bridge.server_results_since_last_decision(
            tuple(outstanding_attempt_ids)
        )
        trace.append(
            {
                "command": "server_results",
                "phase": "post_advance",
                "epoch": epoch,
                "result": asdict(result_batch),
            }
        )
        outstanding_attempt_ids = list(result_batch.pending_attempt_ids)
        if advanced.get("finished") is True:
            break
    if outstanding_attempt_ids:
        raise FuryBridgePlatformEquivalenceError(
            "fixed trace ended with unresolved result-bearing attempts: "
            + ", ".join(outstanding_attempt_ids)
        )
    return trace


def first_difference(left: Any, right: Any, path: str = "$") -> JSONMap | None:
    """Return the first structural difference in deterministic traversal order."""

    if type(left) is not type(right):
        return {
            "path": path,
            "reason": "type",
            "windows": left,
            "linux": right,
        }
    if isinstance(left, dict):
        left_keys = sorted(left)
        right_keys = sorted(right)
        if left_keys != right_keys:
            return {
                "path": path,
                "reason": "keys",
                "windows": left_keys,
                "linux": right_keys,
            }
        for key in left_keys:
            difference = first_difference(left[key], right[key], f"{path}.{key}")
            if difference is not None:
                return difference
        return None
    if isinstance(left, list):
        if len(left) != len(right):
            return {
                "path": path,
                "reason": "length",
                "windows": len(left),
                "linux": len(right),
            }
        for index, (left_value, right_value) in enumerate(zip(left, right)):
            difference = first_difference(
                left_value, right_value, f"{path}[{index}]"
            )
            if difference is not None:
                return difference
        return None
    if left != right:
        return {
            "path": path,
            "reason": "value",
            "windows": left,
            "linux": right,
        }
    return None


def compare_traces(windows_trace: list[JSONMap], linux_trace: list[JSONMap]) -> JSONMap:
    windows_bytes = canonical_bytes(windows_trace)
    linux_bytes = canonical_bytes(linux_trace)
    exact = windows_bytes == linux_bytes
    return {
        "status": "PASS_EXACT_TRACE" if exact else "FAIL_TRACE_DIFFERENCE",
        "exact_canonical_json": exact,
        "windows_trace_sha256": sha256_bytes(windows_bytes),
        "linux_trace_sha256": sha256_bytes(linux_bytes),
        "windows_step_count": len(windows_trace),
        "linux_step_count": len(linux_trace),
        "first_difference": None
        if exact
        else first_difference(windows_trace, linux_trace),
    }


def run_equivalence(
    *,
    request_path: Path,
    windows_bridge: Path,
    linux_bridge: Path,
    simulator_root: Path,
    distro: str,
    seed: int,
    decision_count: int,
    bridge_factory: Callable[..., SimulatorBridge] = SimulatorBridge,
) -> JSONMap:
    request_identity, request = load_request(request_path)
    windows_identity, _ = snapshot_file(windows_bridge)
    linux_identity, _ = snapshot_file(linux_bridge)
    simulator_root = simulator_root.expanduser().resolve()
    if not simulator_root.is_dir():
        raise FuryBridgePlatformEquivalenceError(
            f"simulator root is not a directory: {simulator_root}"
        )

    with bridge_factory(windows_bridge, cwd=simulator_root) as bridge:
        windows_trace = capture_trace(
            bridge, request, seed=seed, decision_count=decision_count
        )
    arguments = linux_wsl_arguments(
        distro=distro,
        simulator_root=simulator_root,
        linux_bridge=linux_bridge,
    )
    with bridge_factory("wsl.exe", arguments=arguments, cwd=simulator_root) as bridge:
        linux_trace = capture_trace(
            bridge, request, seed=seed, decision_count=decision_count
        )

    comparison = compare_traces(windows_trace, linux_trace)
    return {
        "schema": SCHEMA,
        "evidence_scope": EVIDENCE_SCOPE,
        "status": comparison["status"],
        "request": asdict(request_identity),
        "seed": seed,
        "action_rule": ACTION_RULE,
        "decision_count_requested": decision_count,
        "binaries": {
            "windows": asdict(windows_identity),
            "linux_wsl": asdict(linux_identity),
        },
        "launch": {
            "windows_cwd": str(simulator_root),
            "linux_distro": distro,
            "linux_arguments": list(arguments),
        },
        "comparison": comparison,
        "claim_boundary": {
            "supports": [
                "exact canonical request/response trace equality for this fixed request, seed, action rule, and binary pair"
            ],
            "does_not_support": [
                "scientific simulator equivalence",
                "policy fidelity",
                "policy superiority",
                "HPC deployment readiness",
                "real-game correctness",
            ],
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--windows-bridge", type=Path, required=True)
    parser.add_argument("--linux-bridge", type=Path, required=True)
    parser.add_argument("--simulator-root", type=Path, required=True)
    parser.add_argument("--distro", default="Ubuntu-22.04")
    parser.add_argument("--seed", type=int, default=20260910)
    parser.add_argument("--decisions", type=int, default=12)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = run_equivalence(
        request_path=args.request,
        windows_bridge=args.windows_bridge,
        linux_bridge=args.linux_bridge,
        simulator_root=args.simulator_root,
        distro=args.distro,
        seed=args.seed,
        decision_count=args.decisions,
    )
    encoded = json.dumps(
        report, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
    ) + "\n"
    if args.output is not None:
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded, encoding="utf-8", newline="\n")
    print(encoded, end="")
    return 0 if report["status"] == "PASS_EXACT_TRACE" else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__: Sequence[str] = (
    "ACTION_RULE",
    "EVIDENCE_SCOPE",
    "FuryBridgePlatformEquivalenceError",
    "SCHEMA",
    "canonical_bytes",
    "capture_trace",
    "compare_traces",
    "first_difference",
    "linux_wsl_arguments",
    "load_request",
    "run_equivalence",
    "snapshot_file",
    "windows_path_to_wsl",
)
