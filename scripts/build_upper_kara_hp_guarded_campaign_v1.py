"""Extract v5 anchors and build the fresh v6 HP-routing campaign.

The v5 held-out result is used only to choose a predeclared architecture.  The
new campaign therefore uses fresh train/evaluation seeds and routes only on
current target HP.  ``first_wave_arrival_ms`` remains an environment input for
the balanced scenario panel; it is never exposed to the policy search spec.

This script has two local/artifact-only subcommands:

* ``extract-registry`` reads one complete v5 training terminal and writes the
  four exact prototype receipts used by v6; and
* ``build-campaign`` combines that registry with the v5 campaign's frozen
  parent and unchanged simulator settings.

Both outputs are atomic create-only JSON artifacts.  No remote operation is
performed here.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.causal_action_program_v1 import (
    CausalActionProgramV1,
    ProgramOriginV1,
    causal_action_program_from_dict_v1,
)
from o2o_dps.upper_kara_cat_hp_guarded_sparse_routing_search_v1 import (
    FRESH_ARRIVAL_SCHEDULE_MS_V1,
    FRESH_EVALUATION_SEED_START_V1,
    FRESH_SEED_COUNT_V1,
    FRESH_TRAIN_SEED_START_V1,
    FreshSeedProtocolV1,
    HP_GUARDED_SPARSE_ROUTING_PROGRAM_COUNT_V1,
    HP_THRESHOLD_GRID_V1,
    V5_PROTOTYPE_INDEXES_V1,
    build_upper_kara_cat_hp_guarded_sparse_routing_candidate_set_v1,
)
from o2o_dps.upper_kara_causal_program_remote_contract_v1 import (
    CAMPAIGN_SCHEMA,
    FIXED_PARENT_CAT_HP_GUARDED_SPARSE_ROUTING_KIND,
    TERMINAL_COMPLETE,
    FixedParentQueueGcdBlockSearchV1,
    assign_training_shards_v1,
    campaign_from_dict_v1,
    load_continuous_two_wave_remote_campaign_v1,
)
from o2o_dps.upper_kara_causal_program_remote_worker_v1 import (
    _campaign_contract_v1 as campaign_contract_v1,
)


JSONMap = dict[str, Any]
TRAIN_TERMINAL_SCHEMA = "upper_kara_causal_program_remote_train_shard/v1"
PROTOTYPE_REGISTRY_SCHEMA = "upper_kara_v5_sparse_prototype_registry/v1"
HP_ROUTING_SEARCH_KIND = FIXED_PARENT_CAT_HP_GUARDED_SPARSE_ROUTING_KIND
V6_CAMPAIGN_ID = "upper-kara-hp-guarded-routing-v6-256x256"
V6_PROGRAM_COUNT = HP_GUARDED_SPARSE_ROUTING_PROGRAM_COUNT_V1

_REGISTRY_FIELDS = frozenset(
    {
        "schema",
        "campaign_id",
        "build_id",
        "campaign_contract",
        "loadout_id",
        "source_seed_shard_index",
        "prototypes",
    }
)
_PROTOTYPE_RECEIPT_FIELDS = frozenset(
    {
        "prototype_index",
        "program_ref",
        "program_id",
        "program_key",
        "program_origin",
        "proposal_guide_ids",
        "program",
    }
)
_TRAIN_PROGRAM_RECEIPT_FIELDS = frozenset(
    _PROTOTYPE_RECEIPT_FIELDS - {"prototype_index"}
)


def _read_json_object_v1(path: str | Path, label: str) -> JSONMap:
    source = Path(path).expanduser().resolve(strict=True)
    try:
        value = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read {label}: {error}") from error
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    return dict(value)


def _json_bytes_v1(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def write_json_create_only_v1(
    path: str | Path, value: Mapping[str, Any]
) -> Path:
    """Publish one complete JSON artifact without replacing an old result."""

    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_json_bytes_v1(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination.resolve(strict=True)


def _proposal_guide_ids_v1(program: CausalActionProgramV1) -> list[str]:
    return [
        source_ref.removeprefix("proposal-guide:")
        for source_ref in program.source_refs
        if source_ref.startswith("proposal-guide:")
    ]


def _expected_prototype_program_id_v1(
    loadout_id: str, prototype_index: int
) -> str:
    return (
        f"cat-burst-sparse-queue-gcd::{loadout_id}::"
        f"{prototype_index:04d}"
    )


def _validate_prototype_receipt_v1(
    value: object,
    *,
    loadout_id: str,
    expected_index: int,
) -> tuple[JSONMap, CausalActionProgramV1]:
    if not isinstance(value, Mapping) or set(value) != _PROTOTYPE_RECEIPT_FIELDS:
        raise ValueError(
            "prototype receipt fields differ from the v6 contract"
        )
    row = dict(value)
    if row["prototype_index"] != expected_index:
        raise ValueError("prototype receipt order/index differs from v6")
    try:
        program = causal_action_program_from_dict_v1(row["program"])
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"cannot decode prototype {expected_index}: {error}"
        ) from error
    expected_id = _expected_prototype_program_id_v1(
        loadout_id, expected_index
    )
    guide_ids = _proposal_guide_ids_v1(program)
    if (
        program.program_id != expected_id
        or program.origin is not ProgramOriginV1.SEARCHED
        or row["program_ref"] != expected_id
        or row["program_id"] != expected_id
        or row["program_key"] != program.program_key()
        or row["program_origin"] != program.origin.value
        or row["proposal_guide_ids"] != guide_ids
    ):
        raise ValueError(
            f"prototype {expected_index} receipt identity mismatch"
        )
    return row, program


def _validate_registry_v1(
    value: object,
    *,
    v5_campaign: Any,
) -> tuple[JSONMap, dict[int, CausalActionProgramV1]]:
    if not isinstance(value, Mapping) or set(value) != _REGISTRY_FIELDS:
        raise ValueError("prototype registry fields differ from v1")
    row = dict(value)
    spec = v5_campaign.search_spec
    if not isinstance(spec, FixedParentQueueGcdBlockSearchV1):
        raise ValueError("source campaign is not the fixed-parent v5 search")
    if (
        row["schema"] != PROTOTYPE_REGISTRY_SCHEMA
        or row["campaign_id"] != v5_campaign.campaign_id
        or row["build_id"] != v5_campaign.build_id
        or row["campaign_contract"] != campaign_contract_v1(v5_campaign)
        or row["loadout_id"] != spec.parent_loadout_id
    ):
        raise ValueError("prototype registry source identity mismatch")
    shard_index = row["source_seed_shard_index"]
    if (
        isinstance(shard_index, bool)
        or not isinstance(shard_index, int)
        or not 0 <= shard_index < v5_campaign.seed_shard_count
    ):
        raise ValueError("source_seed_shard_index is outside the v5 campaign")
    raw_prototypes = row["prototypes"]
    if not isinstance(raw_prototypes, list) or len(raw_prototypes) != len(
        V5_PROTOTYPE_INDEXES_V1
    ):
        raise ValueError("registry must contain exactly four prototypes")
    prototypes: dict[int, CausalActionProgramV1] = {}
    parsed_receipts: list[JSONMap] = []
    for raw, expected_index in zip(
        raw_prototypes, V5_PROTOTYPE_INDEXES_V1, strict=True
    ):
        receipt, program = _validate_prototype_receipt_v1(
            raw,
            loadout_id=spec.parent_loadout_id,
            expected_index=expected_index,
        )
        parsed_receipts.append(receipt)
        prototypes[expected_index] = program
    candidate_set = (
        build_upper_kara_cat_hp_guarded_sparse_routing_candidate_set_v1(
            loadout_id=spec.parent_loadout_id,
            parent_program=spec.parent_program,
            prototype_programs=prototypes,
        )
    )
    if (
        len(candidate_set.programs) != V6_PROGRAM_COUNT
        or list(HP_THRESHOLD_GRID_V1) != [20, 35, 50, 65, 80]
    ):
        raise AssertionError("the frozen v6 candidate family drifted")
    row["prototypes"] = parsed_receipts
    return row, prototypes


def load_prototype_registry_v1(
    path: str | Path,
    *,
    v5_campaign: Any,
) -> JSONMap:
    registry, _ = _validate_registry_v1(
        _read_json_object_v1(path, "prototype registry"),
        v5_campaign=v5_campaign,
    )
    return registry


def extract_prototype_registry_v1(
    *,
    v5_campaign_path: str | Path,
    train_terminal_path: str | Path,
) -> JSONMap:
    """Extract exact v5 receipts 42/47/159/262 from one terminal."""

    campaign = load_continuous_two_wave_remote_campaign_v1(v5_campaign_path)
    spec = campaign.search_spec
    if not isinstance(spec, FixedParentQueueGcdBlockSearchV1):
        raise ValueError("source campaign is not the fixed-parent v5 search")
    terminal = _read_json_object_v1(train_terminal_path, "v5 train terminal")
    shard_index = terminal.get("seed_shard_index")
    shards = {
        shard.seed_shard_index: shard
        for shard in assign_training_shards_v1(campaign)
        if shard.loadout_id == spec.parent_loadout_id
    }
    shard = shards.get(shard_index)
    if (
        terminal.get("schema") != TRAIN_TERMINAL_SCHEMA
        or terminal.get("terminal_status") != TERMINAL_COMPLETE
        or terminal.get("campaign_id") != campaign.campaign_id
        or terminal.get("build_id") != campaign.build_id
        or terminal.get("campaign_contract") != campaign_contract_v1(campaign)
        or terminal.get("loadout_id") != spec.parent_loadout_id
        or shard is None
        or terminal.get("examples")
        != [example.to_dict() for example in shard.examples]
    ):
        raise ValueError("v5 train terminal identity is incomplete or mismatched")
    raw_programs = terminal.get("programs")
    if not isinstance(raw_programs, list) or not raw_programs:
        raise ValueError("v5 train terminal has no program receipts")

    by_program_id: dict[str, Mapping[str, Any]] = {}
    program_refs: set[str] = set()
    program_keys: set[str] = set()
    for raw in raw_programs:
        if (
            not isinstance(raw, Mapping)
            or set(raw) != _TRAIN_PROGRAM_RECEIPT_FIELDS
        ):
            raise ValueError("v5 train terminal has a malformed program receipt")
        program_id = raw.get("program_id")
        program_ref = raw.get("program_ref")
        program_key = raw.get("program_key")
        if (
            not isinstance(program_id, str)
            or not isinstance(program_ref, str)
            or not isinstance(program_key, str)
            or program_id in by_program_id
            or program_ref in program_refs
            or program_key in program_keys
        ):
            raise ValueError(
                "v5 train terminal program identities are invalid or repeated"
            )
        by_program_id[program_id] = raw
        program_refs.add(program_ref)
        program_keys.add(program_key)

    parent_raw = by_program_id.get(spec.parent_program.program_id)
    if not isinstance(parent_raw, Mapping):
        raise ValueError("v5 train terminal does not contain its exact parent")
    parent = causal_action_program_from_dict_v1(parent_raw.get("program"))
    if (
        parent != spec.parent_program
        or parent_raw.get("program_ref") != parent.program_id
        or parent_raw.get("program_key") != parent.program_key()
        or parent_raw.get("program_origin") != parent.origin.value
    ):
        raise ValueError("v5 train terminal parent receipt identity mismatch")

    prototypes: list[JSONMap] = []
    for prototype_index in V5_PROTOTYPE_INDEXES_V1:
        expected_id = _expected_prototype_program_id_v1(
            spec.parent_loadout_id, prototype_index
        )
        raw = by_program_id.get(expected_id)
        if not isinstance(raw, Mapping):
            raise ValueError(f"v5 terminal is missing prototype {prototype_index}")
        candidate = {
            "prototype_index": prototype_index,
            "program_ref": raw.get("program_ref"),
            "program_id": raw.get("program_id"),
            "program_key": raw.get("program_key"),
            "program_origin": raw.get("program_origin"),
            "proposal_guide_ids": raw.get("proposal_guide_ids"),
            "program": raw.get("program"),
        }
        parsed, _ = _validate_prototype_receipt_v1(
            candidate,
            loadout_id=spec.parent_loadout_id,
            expected_index=prototype_index,
        )
        prototypes.append(parsed)

    registry: JSONMap = {
        "schema": PROTOTYPE_REGISTRY_SCHEMA,
        "campaign_id": campaign.campaign_id,
        "build_id": campaign.build_id,
        "campaign_contract": campaign_contract_v1(campaign),
        "loadout_id": spec.parent_loadout_id,
        "source_seed_shard_index": shard_index,
        "prototypes": prototypes,
    }
    validated, _ = _validate_registry_v1(registry, v5_campaign=campaign)
    return validated


def build_hp_guarded_campaign_wire_v1(
    *,
    v5_campaign_path: str | Path,
    prototype_registry_path: str | Path,
    campaign_id: str = V6_CAMPAIGN_ID,
) -> JSONMap:
    """Build and contract-round-trip the fresh 256x256 v6 campaign."""

    v5 = load_continuous_two_wave_remote_campaign_v1(v5_campaign_path)
    spec = v5.search_spec
    if not isinstance(spec, FixedParentQueueGcdBlockSearchV1):
        raise ValueError("source campaign is not the fixed-parent v5 search")
    registry = load_prototype_registry_v1(
        prototype_registry_path, v5_campaign=v5
    )
    protocol = FreshSeedProtocolV1()
    if (
        protocol.train_seed_start != FRESH_TRAIN_SEED_START_V1
        or protocol.evaluation_seed_start != FRESH_EVALUATION_SEED_START_V1
        or protocol.seed_count != FRESH_SEED_COUNT_V1
        or protocol.arrival_schedule_ms != FRESH_ARRIVAL_SCHEDULE_MS_V1
    ):
        raise AssertionError("fresh v6 seed protocol drifted")
    wire: JSONMap = {
        "schema": CAMPAIGN_SCHEMA,
        "campaign_id": campaign_id,
        "build_id": v5.build_id,
        "train_examples": [
            {"seed": seed, "first_wave_arrival_ms": arrival}
            for seed, arrival in protocol.examples("train")
        ],
        "evaluation_examples": [
            {"seed": seed, "first_wave_arrival_ms": arrival}
            for seed, arrival in protocol.examples("evaluation")
        ],
        "generation_config": v5.generation_config.to_dict(),
        "loadout_ids": [spec.parent_loadout_id],
        "seed_shard_count": FRESH_SEED_COUNT_V1,
        "pull_time_ms": v5.pull_time_ms,
        "max_decisions": v5.max_decisions,
        "search_spec": {
            "kind": HP_ROUTING_SEARCH_KIND,
            "parent_loadout_id": spec.parent_loadout_id,
            "parent_program": spec.parent_program.to_dict(),
            "prototype_programs": registry["prototypes"],
            "max_programs": V6_PROGRAM_COUNT,
            "hp_thresholds": list(HP_THRESHOLD_GRID_V1),
        },
    }
    # The shared campaign contract owns the authoritative wire validation and
    # derives its audit block.  This also prevents the builder from becoming a
    # second, looser implementation of the remote contract.
    parsed = campaign_from_dict_v1(wire)
    result = parsed.to_dict()
    if (
        [row["seed"] for row in result["train_examples"]]
        != list(range(720_001, 720_257))
        or [row["seed"] for row in result["evaluation_examples"]]
        != list(range(820_001, 820_257))
        or result["search_spec"] != wire["search_spec"]
    ):
        raise AssertionError("v6 campaign round trip changed frozen inputs")
    return result


def _parser_v1() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    extract = subparsers.add_parser(
        "extract-registry", help="extract four exact receipts from one v5 terminal"
    )
    extract.add_argument("--v5-campaign", type=Path, required=True)
    extract.add_argument("--train-terminal", type=Path, required=True)
    extract.add_argument("--output", type=Path, required=True)

    build = subparsers.add_parser(
        "build-campaign", help="build the fresh v6 HP-routing campaign"
    )
    build.add_argument("--v5-campaign", type=Path, required=True)
    build.add_argument("--prototype-registry", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--campaign-id", default=V6_CAMPAIGN_ID)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser_v1().parse_args(argv)
    if args.command == "extract-registry":
        value = extract_prototype_registry_v1(
            v5_campaign_path=args.v5_campaign,
            train_terminal_path=args.train_terminal,
        )
    elif args.command == "build-campaign":
        value = build_hp_guarded_campaign_wire_v1(
            v5_campaign_path=args.v5_campaign,
            prototype_registry_path=args.prototype_registry,
            campaign_id=args.campaign_id,
        )
    else:  # pragma: no cover - argparse keeps this unreachable.
        raise AssertionError(f"unsupported command {args.command!r}")
    print(write_json_create_only_v1(args.output, value))


if __name__ == "__main__":
    main()
