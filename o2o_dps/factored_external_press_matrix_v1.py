"""Small matrix worker for the factored external-press development search.

One worker owns exactly one rank, frozen wave stratum, phase, and seed index.
Training artifacts are reduced into proposals; held-out transfer artifacts may
authorize an exact route; untouched artifacts then evaluate only authorized
rules.  The JSON artifacts contain a small route-reconstruction projection,
not Chronicle rows or simulator checkpoints.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import re
from statistics import mean
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Mapping

from .cat_external_press_action_teacher_v1 import (
    DEFAULT_BRIDGE,
    PROJECT_ROOT,
    STATE_SELECTION_CONTRACT,
    run_cat_external_press_action_teacher_v1,
)
from .conditional_cat_branch_v1 import FrozenRuleV1
from .development_historical_build_wave_case_v1 import DEFAULT_ITEM_DATABASE
from .development_wave_case_v1 import DevelopmentWaveCaseV1
from .development_wave_stratified_v1 import WAVE_STRATA, build_stratified_wave_case_v1
from .development_wave_team_retarget_v1 import V14ProjectedDynamicV3Bridge
from .factored_cat_branch_router_v1 import (
    authorize_factored_routes_v1,
    fit_factored_cat_branch_router_v1,
    mechanism_route_v1,
    proposed_rule_for_case_v1,
    rule_for_case_v1,
)
from .factored_external_press_search_v1 import _evaluate_pair
from .factored_historical_build_wave_case_v1 import (
    bind_historical_build_to_wave_case_v1,
)
from .factored_sparse_guard_learner_v2 import (
    fit_factored_sparse_guard_learner_v2,
)
from .historical_representative_character_profile_v1 import (
    DEFAULT_SELECTOR_MANIFEST,
)


SCHEMA = "factored_external_press_matrix_case/v1"
PROPOSAL_SCHEMA = "factored_external_press_matrix_proposal/v1"
AUTHORIZATION_SCHEMA = "factored_external_press_matrix_authorization/v1"
FRESH_SCHEMA = "factored_external_press_matrix_fresh/v1"
BATCH_SCHEMA = "factored_external_press_matrix_batch/v1"
SPARSE_SHORTLIST_SCHEMA = "factored_external_press_matrix_sparse_shortlist/v5"
EXECUTION_CONTRACT_SCHEMA = "factored_external_press_matrix_execution_contract/v1"
REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE = "ATOMIC_DYNAMIC_V3_PRESS_CLOCK"

# Order is part of the frozen matrix and the seed namespace.
MATRIX_RANKS = (7, 11, 9)
MATRIX_STRATA = ("q05", "q60", "q95", "multi_2")
MATRIX_PHASES = ("training", "held_out_transfer", "untouched_fresh")
STRATUM_BINDINGS = {
    "q05": "single_short",
    "q60": "single_medium",
    "q95": "single_long",
    "multi_2": "multi_two",
}
PHASE_SEED_BASES = {
    "training": 261_000_000_000,
    "held_out_transfer": 262_000_000_000,
    "untouched_fresh": 263_000_000_000,
}
PHASE_SEED_CAPACITY = 1_000_000
MAX_SAMPLE_INDEX = PHASE_SEED_CAPACITY - 1
DEFAULT_BRIDGE_CWD = PROJECT_ROOT.parent / "wowsims-turtle"
_BATCH_CONTEXT: dict[str, Any] | None = None


@dataclass(frozen=True)
class _ReducerCaseV1:
    """Duck-typed case containing only fields used by the factored router."""

    case_spec: dict[str, Any]
    request: dict[str, Any]
    dynamic_load: Any


def _resolved_path(path: Path | None) -> str | None:
    return str(path.expanduser().resolve()) if path is not None else None


def _bridge_version_tag(path: Path) -> str:
    match = re.search(r"(?:^|[^a-z0-9])v(\d+)(?:[^0-9]|$)", path.name, re.IGNORECASE)
    return f"V{match.group(1)}" if match else f"UNVERSIONED:{path.name}"


def _declared_profile_paths(
    *, selector_manifest_path: Path, representatives_path: Path | None,
    catalog_manifest_path: Path | None, catalog_data_path: Path | None,
) -> tuple[Path | None, Path | None, Path | None]:
    """Resolve the selector-declared defaults when explicit relocations are absent."""

    if all(path is not None for path in (
        representatives_path, catalog_manifest_path, catalog_data_path,
    )):
        return representatives_path, catalog_manifest_path, catalog_data_path
    manifest = _read_json(selector_manifest_path)
    outputs = manifest.get("outputs")
    inputs = manifest.get("inputs")
    if not isinstance(outputs, Mapping) or not isinstance(inputs, Mapping):
        raise ValueError("selector manifest lacks declared input/output paths")

    def declared(raw: Any) -> Path | None:
        if not isinstance(raw, str) or not raw:
            return None
        value = Path(raw)
        return value if value.is_absolute() else selector_manifest_path.parent / value

    return (
        representatives_path or declared(outputs.get("representatives_path")),
        catalog_manifest_path or declared(inputs.get("catalog_manifest")),
        catalog_data_path or declared(inputs.get("catalog_path")),
    )


def _execution_contract_v1(
    *, phase: str, case: DevelopmentWaveCaseV1,
    item_database: Mapping[str, Any], router: Mapping[str, Any] | None,
    period_ms: int, max_states: int, max_presses: int,
    bridge_path: Path, bridge_cwd: Path, item_database_path: Path,
    selector_manifest_path: Path, representatives_path: Path | None,
    catalog_manifest_path: Path | None, catalog_data_path: Path | None,
    router_path: Path | None,
) -> dict[str, Any]:
    representatives_path, catalog_manifest_path, catalog_data_path = (
        _declared_profile_paths(
            selector_manifest_path=selector_manifest_path,
            representatives_path=representatives_path,
            catalog_manifest_path=catalog_manifest_path,
            catalog_data_path=catalog_data_path,
        )
    )
    if phase == "training":
        if router is not None or router_path is not None:
            raise ValueError("training execution contract must not have router lineage")
        lineage = {
            "router_wrapper_schema": None,
            "mechanism_route": None,
            "selected_rule": None,
        }
    else:
        if not isinstance(router, Mapping):
            raise ValueError(f"{phase} execution contract requires a router")
        selector = (
            proposed_rule_for_case_v1
            if phase == "held_out_transfer" else rule_for_case_v1
        )
        route, rule = selector(router, case, item_database=item_database)
        lineage = {
            "router_wrapper_schema": (
                PROPOSAL_SCHEMA if phase == "held_out_transfer"
                else AUTHORIZATION_SCHEMA
            ),
            "mechanism_route": asdict(route),
            "selected_rule": asdict(rule),
        }
    semantic = {
        "period_ms": period_ms,
        "max_states": max_states,
        "max_presses": max_presses,
        "teacher_state_selection_contract": STATE_SELECTION_CONTRACT,
        "required_press_clock_configuration_mode": (
            REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE
        ),
        "bridge_version_tag": _bridge_version_tag(bridge_path),
        "input_artifact_names": {
            "item_database": item_database_path.name,
            "selector_manifest": selector_manifest_path.name,
            "representatives": (
                representatives_path.name if representatives_path is not None else None
            ),
            "catalog_manifest": (
                catalog_manifest_path.name if catalog_manifest_path is not None else None
            ),
            "catalog_data": catalog_data_path.name if catalog_data_path is not None else None,
        },
    }
    site = {
        "bridge_path": _resolved_path(bridge_path),
        "bridge_cwd": _resolved_path(bridge_cwd),
        "item_database_path": _resolved_path(item_database_path),
        "selector_manifest_path": _resolved_path(selector_manifest_path),
        "representatives_path": _resolved_path(representatives_path),
        "catalog_manifest_path": _resolved_path(catalog_manifest_path),
        "catalog_data_path": _resolved_path(catalog_data_path),
        "router_path": _resolved_path(router_path),
    }
    return {
        "schema": EXECUTION_CONTRACT_SCHEMA,
        "semantic": semantic,
        "site": site,
        "router_lineage": lineage,
    }


def _execution_semantic(contract: Mapping[str, Any]) -> dict[str, Any]:
    if contract.get("schema") != EXECUTION_CONTRACT_SCHEMA:
        raise ValueError("matrix execution contract schema differs")
    semantic = contract.get("semantic")
    site = contract.get("site")
    lineage = contract.get("router_lineage")
    if not all(isinstance(value, Mapping) for value in (semantic, site, lineage)):
        raise ValueError("matrix execution contract is incomplete")
    if semantic.get("required_press_clock_configuration_mode") != (
        REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE
    ):
        raise ValueError("matrix execution contract does not require atomic press clock")
    teacher_selection_contract = semantic.get("teacher_state_selection_contract")
    if teacher_selection_contract is not None and (
        not isinstance(teacher_selection_contract, str)
        or not teacher_selection_contract
    ):
        raise ValueError("matrix teacher state-selection contract is invalid")
    return deepcopy(dict(semantic))


def _result_clock_modes(
    result: Mapping[str, Any], phase: str,
) -> Mapping[str, Any] | None:
    if phase != "training":
        value = result.get("press_clock_configuration_modes")
        return value if isinstance(value, Mapping) else None
    modes = {"baseline": result.get("baseline_press_clock_configuration_mode")}
    for index, branch in enumerate(result.get("branches") or []):
        if not isinstance(branch, Mapping):
            return None
        modes[f"branch_{index}"] = branch.get("press_clock_configuration_mode")
    return modes


def matrix_seed_v1(
    rank: int, stratum: str, phase: str, sample_index: int,
) -> int:
    """Return the phase-disjoint randomized-block seed for one sample.

    A sample seed is deliberately reused across all twelve rank/stratum cells.
    The cell tuple remains the artifact key; the router therefore counts one
    randomized block as one seed instead of twelve pseudo-replicates.
    """

    if rank not in MATRIX_RANKS:
        raise ValueError(f"rank must be one of {MATRIX_RANKS}")
    if stratum not in MATRIX_STRATA:
        raise ValueError(f"stratum must be one of {MATRIX_STRATA}")
    if phase not in MATRIX_PHASES:
        raise ValueError(f"phase must be one of {MATRIX_PHASES}")
    if type(sample_index) is not int or not 0 <= sample_index <= MAX_SAMPLE_INDEX:
        raise ValueError(f"sample_index must be in [0, {MAX_SAMPLE_INDEX}]")
    return PHASE_SEED_BASES[phase] + sample_index


def _profile_overrides(
    *, representatives_path: Path | None,
    catalog_manifest_path: Path | None,
    catalog_data_path: Path | None,
) -> dict[str, Path] | None:
    rows = {
        "representatives": representatives_path,
        "catalog_manifest": catalog_manifest_path,
        "catalog_data": catalog_data_path,
    }
    result = {key: value for key, value in rows.items() if value is not None}
    return result or None


def build_matrix_case_v1(
    rank: int, stratum: str, phase: str, sample_index: int, *,
    item_database_path: Path = DEFAULT_ITEM_DATABASE,
    selector_manifest_path: Path = DEFAULT_SELECTOR_MANIFEST,
    representatives_path: Path | None = None,
    catalog_manifest_path: Path | None = None,
    catalog_data_path: Path | None = None,
) -> DevelopmentWaveCaseV1:
    """Bind one exact representative build to one frozen model wave."""

    seed = matrix_seed_v1(rank, stratum, phase, sample_index)
    return build_matrix_case_from_seed_v1(
        rank, stratum, seed,
        item_database_path=item_database_path,
        selector_manifest_path=selector_manifest_path,
        representatives_path=representatives_path,
        catalog_manifest_path=catalog_manifest_path,
        catalog_data_path=catalog_data_path,
    )


def build_matrix_case_from_seed_v1(
    rank: int, stratum: str, seed: int, *,
    item_database_path: Path = DEFAULT_ITEM_DATABASE,
    selector_manifest_path: Path = DEFAULT_SELECTOR_MANIFEST,
    representatives_path: Path | None = None,
    catalog_manifest_path: Path | None = None,
    catalog_data_path: Path | None = None,
) -> DevelopmentWaveCaseV1:
    """Bind the frozen rank/stratum cell to an explicitly owned seed namespace."""

    if rank not in MATRIX_RANKS:
        raise ValueError(f"rank must be one of {MATRIX_RANKS}")
    if stratum not in MATRIX_STRATA:
        raise ValueError(f"stratum must be one of {MATRIX_STRATA}")
    if type(seed) is not int or seed < 0:
        raise ValueError("seed must be a nonnegative integer")
    destination, _ = build_stratified_wave_case_v1(
        seed, STRATUM_BINDINGS[stratum],
    )
    return bind_historical_build_to_wave_case_v1(
        destination,
        rank=rank,
        item_database_path=item_database_path,
        selector_manifest_path=selector_manifest_path,
        profile_path_overrides=_profile_overrides(
            representatives_path=representatives_path,
            catalog_manifest_path=catalog_manifest_path,
            catalog_data_path=catalog_data_path,
        ),
    )


def _load_item_database(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("items"), list):
        raise ValueError("item database must be a JSON object with an items list")
    return value


def _case_projection(
    case: DevelopmentWaveCaseV1, *, rank: int, stratum: str,
) -> dict[str, Any]:
    """Retain only the current router inputs and immutable case identities."""

    player = case.request["raid"]["parties"][0]["players"][0]
    items = player["equipment"]["items"]
    if len(items) < 16:
        raise ValueError("matrix case lacks complete equipment slots")
    transplant = case.case_spec.get("build_transplant") or {}
    if transplant.get("representative_rank") != rank:
        raise ValueError("matrix rank differs from bound representative rank")
    projected_items = [
        ({"id": item["id"]} if isinstance(item, Mapping) and type(item.get("id")) is int else {})
        for item in items[:16]
    ]
    spec_fields = (
        "source_wave_ref", "build_ref", "build_transplant", "initial_state",
        "team_background", "two_wave_model",
    )
    return {
        "schema": "factored_external_press_matrix_case_projection/v1",
        "rank": rank,
        "stratum": stratum,
        "seed": case.dynamic_load.seed,
        "request_sha256": case.dynamic_load.request_sha256,
        "dynamic_load_contract_sha256": case.dynamic_load.contract_sha256,
        "case_spec": {
            key: deepcopy(case.case_spec[key])
            for key in spec_fields if key in case.case_spec
        },
        "request": {
            "raid": {"parties": [{"players": [{
                "equipment": {"items": projected_items},
                "talentsString": player.get("talentsString"),
            }]}]},
            "encounter": {
                "targets": [{} for _ in case.request["encounter"]["targets"]],
            },
        },
    }


def _case_from_projection(value: Mapping[str, Any]) -> _ReducerCaseV1:
    if value.get("schema") != "factored_external_press_matrix_case_projection/v1":
        raise ValueError("matrix case projection schema differs")
    load = SimpleNamespace(
        seed=value.get("seed"),
        request_sha256=value.get("request_sha256"),
        contract_sha256=value.get("dynamic_load_contract_sha256"),
    )
    case_spec = value.get("case_spec")
    request = value.get("request")
    if not isinstance(case_spec, dict) or not isinstance(request, dict):
        raise ValueError("matrix case projection is incomplete")
    return _ReducerCaseV1(deepcopy(case_spec), deepcopy(request), load)


def execute_matrix_case_v1(
    case: DevelopmentWaveCaseV1, *, rank: int, stratum: str, phase: str,
    sample_index: int, bridge_factory: Callable[[], Any],
    item_database: Mapping[str, Any], router: Mapping[str, Any] | None = None,
    period_ms: int = 100, max_states: int = 1, max_presses: int = 400,
    execution_contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute one isolated worker artifact; fitting never occurs here."""

    expected_seed = matrix_seed_v1(rank, stratum, phase, sample_index)
    if case.dynamic_load.seed != expected_seed:
        raise ValueError("case seed differs from the frozen matrix namespace")
    if case.case_spec.get("source_wave_ref") != WAVE_STRATA[STRATUM_BINDINGS[stratum]]:
        raise ValueError("case wave differs from the frozen matrix stratum")
    route = mechanism_route_v1(case, item_database=item_database)
    if execution_contract is None:
        execution_contract = _execution_contract_v1(
            phase=phase, case=case, item_database=item_database, router=router,
            period_ms=period_ms, max_states=max_states, max_presses=max_presses,
            bridge_path=DEFAULT_BRIDGE, bridge_cwd=DEFAULT_BRIDGE_CWD,
            item_database_path=DEFAULT_ITEM_DATABASE,
            selector_manifest_path=DEFAULT_SELECTOR_MANIFEST,
            representatives_path=None, catalog_manifest_path=None,
            catalog_data_path=None, router_path=None,
        )
    execution_contract = deepcopy(dict(execution_contract))
    semantic = _execution_semantic(execution_contract)
    if (
        semantic.get("period_ms") != period_ms
        or semantic.get("max_states") != max_states
        or semantic.get("max_presses") != max_presses
    ):
        raise ValueError("matrix execution contract differs from worker arguments")
    if phase == "training":
        if router is not None:
            raise ValueError("training worker must not receive a router")
        result = run_cat_external_press_action_teacher_v1(
            case, bridge_factory, period_ms=period_ms, max_states=max_states,
            max_presses=max_presses,
        )
        expected_schema = "cat_external_press_action_teacher/v1"
        if result.get("state_selection_contract") != semantic.get(
            "teacher_state_selection_contract"
        ):
            raise ValueError(
                "teacher result state-selection contract differs from execution contract"
            )
    else:
        if router is None:
            raise ValueError(f"{phase} worker requires its preceding router")
        selector = (
            proposed_rule_for_case_v1
            if phase == "held_out_transfer" else rule_for_case_v1
        )
        selected_route, rule = selector(router, case, item_database=item_database)
        if asdict(selected_route) != asdict(route):
            raise ValueError("worker route changed while selecting its rule")
        result = _evaluate_pair(
            case, FrozenRuleV1(**asdict(rule)), bridge_factory,
            period_ms=period_ms, max_presses=max_presses, simulator_inputs=None,
        )
        result["mechanism_route"] = asdict(route)
        result["phase"] = (
            "HELD_OUT_TRANSFER" if phase == "held_out_transfer"
            else "UNTOUCHED_FRESH"
        )
        expected_schema = "factored_external_press_fresh_pair/v1"
    if result.get("schema") != expected_schema:
        raise ValueError("worker result schema differs from its matrix phase")
    lineage = execution_contract["router_lineage"]
    expected_wrapper_schema = (
        None if phase == "training" else
        PROPOSAL_SCHEMA if phase == "held_out_transfer" else AUTHORIZATION_SCHEMA
    )
    expected_rule = None if phase == "training" else result.get("rule")
    if (
        lineage.get("router_wrapper_schema") != expected_wrapper_schema
        or lineage.get("mechanism_route") != (
            None if phase == "training" else asdict(route)
        )
        or lineage.get("selected_rule") != expected_rule
    ):
        raise ValueError("matrix execution router lineage differs from selected route/rule")
    actual_clock_modes = _result_clock_modes(result, phase)
    if (
        not isinstance(actual_clock_modes, Mapping)
        or not actual_clock_modes
        or any(
            value != REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE
            for value in actual_clock_modes.values()
        )
    ):
        raise ValueError("matrix worker did not use the required atomic press clock")
    return {
        "schema": SCHEMA,
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "status": "COMPLETE_MATRIX_CASE_NONVOTING",
        "matrix": {
            "rank": rank,
            "stratum": stratum,
            "source_stratum": STRATUM_BINDINGS[stratum],
            "phase": phase,
            "sample_index": sample_index,
            "seed": expected_seed,
            "seed_contract": "PHASE_DISJOINT_CROSS_CELL_RANDOMIZED_BLOCK_V1",
        },
        "case_projection": _case_projection(case, rank=rank, stratum=stratum),
        "mechanism_route": asdict(route),
        "execution_contract": execution_contract,
        "result": result,
        "raw_chronicle_rows_loaded": False,
        "voting_eligible": False,
        "deployment_eligible": False,
    }


def run_matrix_case_v1(
    rank: int, stratum: str, phase: str, sample_index: int,
    bridge_factory: Callable[[], Any], *,
    item_database_path: Path = DEFAULT_ITEM_DATABASE,
    selector_manifest_path: Path = DEFAULT_SELECTOR_MANIFEST,
    representatives_path: Path | None = None,
    catalog_manifest_path: Path | None = None,
    catalog_data_path: Path | None = None,
    router: Mapping[str, Any] | None = None,
    period_ms: int = 100, max_states: int = 1, max_presses: int = 400,
    bridge_path: Path = DEFAULT_BRIDGE,
    bridge_cwd: Path = DEFAULT_BRIDGE_CWD,
    router_path: Path | None = None,
) -> dict[str, Any]:
    item_database = _load_item_database(item_database_path)
    case = build_matrix_case_v1(
        rank, stratum, phase, sample_index,
        item_database_path=item_database_path,
        selector_manifest_path=selector_manifest_path,
        representatives_path=representatives_path,
        catalog_manifest_path=catalog_manifest_path,
        catalog_data_path=catalog_data_path,
    )
    execution_contract = _execution_contract_v1(
        phase=phase, case=case, item_database=item_database, router=router,
        period_ms=period_ms, max_states=max_states, max_presses=max_presses,
        bridge_path=bridge_path, bridge_cwd=bridge_cwd,
        item_database_path=item_database_path,
        selector_manifest_path=selector_manifest_path,
        representatives_path=representatives_path,
        catalog_manifest_path=catalog_manifest_path,
        catalog_data_path=catalog_data_path, router_path=router_path,
    )
    return execute_matrix_case_v1(
        case, rank=rank, stratum=stratum, phase=phase,
        sample_index=sample_index, bridge_factory=bridge_factory,
        item_database=item_database, router=router, period_ms=period_ms,
        max_states=max_states, max_presses=max_presses,
        execution_contract=execution_contract,
    )


def _validated_phase_artifacts(
    artifacts: Iterable[Mapping[str, Any]], phase: str,
) -> list[Mapping[str, Any]]:
    rows = list(artifacts)
    if not rows:
        raise ValueError(f"{phase} reducer requires artifacts")
    seen: set[tuple[int, str, int]] = set()
    semantic_contracts: list[dict[str, Any]] = []
    samples: dict[tuple[int, str], set[int]] = {
        (rank, stratum): set()
        for rank in MATRIX_RANKS for stratum in MATRIX_STRATA
    }
    for artifact in rows:
        if artifact.get("schema") != SCHEMA:
            raise ValueError("matrix artifact schema differs")
        matrix = artifact.get("matrix")
        if not isinstance(matrix, Mapping) or matrix.get("phase") != phase:
            raise ValueError("matrix artifact phase differs from reducer phase")
        rank, stratum = matrix.get("rank"), matrix.get("stratum")
        sample_index, seed = matrix.get("sample_index"), matrix.get("seed")
        expected = matrix_seed_v1(rank, stratum, phase, sample_index)
        if seed != expected:
            raise ValueError("matrix artifact seed violates its namespace")
        key = (rank, stratum, sample_index)
        if key in seen:
            raise ValueError("duplicate matrix rank/stratum/sample artifact")
        seen.add(key)
        samples[(rank, stratum)].add(sample_index)
        projection = artifact.get("case_projection")
        result = artifact.get("result")
        execution_contract = artifact.get("execution_contract")
        if (
            not isinstance(projection, Mapping)
            or not isinstance(result, Mapping)
            or not isinstance(execution_contract, Mapping)
        ):
            raise ValueError("matrix artifact lacks projection, result, or execution contract")
        semantic = _execution_semantic(execution_contract)
        semantic_contracts.append(semantic)
        if (
            phase == "training"
            and semantic.get("teacher_state_selection_contract") is not None
            and result.get("state_selection_contract")
            != semantic.get("teacher_state_selection_contract")
        ):
            raise ValueError(
                "matrix teacher state-selection contract differs from execution contract"
            )
        if result.get("period_ms") != semantic.get("period_ms"):
            raise ValueError("matrix result period differs from execution contract")
        expected_wrapper_schema = (
            None if phase == "training" else
            PROPOSAL_SCHEMA if phase == "held_out_transfer" else AUTHORIZATION_SCHEMA
        )
        lineage = execution_contract["router_lineage"]
        if lineage.get("router_wrapper_schema") != expected_wrapper_schema:
            raise ValueError("matrix router wrapper lineage differs from phase")
        clock_modes = _result_clock_modes(result, phase)
        if (
            not isinstance(clock_modes, Mapping)
            or not clock_modes
            or any(
                value != REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE
                for value in clock_modes.values()
            )
        ):
            raise ValueError("matrix artifact lacks atomic press-clock receipts")
        identity = (
            projection.get("seed"), projection.get("request_sha256"),
            projection.get("dynamic_load_contract_sha256"),
        )
        if identity != (
            seed, result.get("request_sha256"),
            result.get("dynamic_load_contract_sha256"),
        ):
            raise ValueError("matrix projection and result identities differ")
        if (
            projection.get("rank") != rank
            or projection.get("stratum") != stratum
            or projection.get("case_spec", {}).get("source_wave_ref")
            != result.get("source_wave_ref")
        ):
            raise ValueError("matrix projection tracking differs from result")
    sample_sets = set(tuple(sorted(value)) for value in samples.values())
    if len(sample_sets) != 1 or next(iter(sample_sets), ()) == ():
        raise ValueError("matrix phase is incomplete or unbalanced across 12 cells")
    if any(value != semantic_contracts[0] for value in semantic_contracts[1:]):
        raise ValueError("matrix phase mixes semantic execution contracts")
    return sorted(rows, key=lambda row: (
        MATRIX_RANKS.index(row["matrix"]["rank"]),
        MATRIX_STRATA.index(row["matrix"]["stratum"]),
        row["matrix"]["sample_index"],
    ))


def _projected_case_and_route(
    artifact: Mapping[str, Any], item_database: Mapping[str, Any],
) -> tuple[_ReducerCaseV1, dict[str, Any]]:
    case = _case_from_projection(artifact["case_projection"])
    route = asdict(mechanism_route_v1(case, item_database=item_database))
    if route != artifact.get("mechanism_route"):
        raise ValueError("stored mechanism route differs from compact case projection")
    phase = artifact["matrix"]["phase"]
    lineage = artifact["execution_contract"]["router_lineage"]
    if phase == "training":
        expected_lineage = {
            "router_wrapper_schema": None,
            "mechanism_route": None,
            "selected_rule": None,
        }
    else:
        expected_lineage = {
            "router_wrapper_schema": (
                PROPOSAL_SCHEMA if phase == "held_out_transfer"
                else AUTHORIZATION_SCHEMA
            ),
            "mechanism_route": route,
            "selected_rule": artifact["result"].get("rule"),
        }
    if dict(lineage) != expected_lineage:
        raise ValueError("stored router lineage differs from compact case projection")
    return case, route


def fit_matrix_proposal_v1(
    training_artifacts: Iterable[Mapping[str, Any]], *,
    item_database: Mapping[str, Any], min_distinct_seeds: int = 6,
) -> dict[str, Any]:
    rows = _validated_phase_artifacts(training_artifacts, "training")
    teacher_cases = []
    for artifact in rows:
        case, _ = _projected_case_and_route(artifact, item_database)
        teacher = artifact["result"]
        if teacher.get("schema") != "cat_external_press_action_teacher/v1":
            raise ValueError("training artifact is not an external-press teacher")
        teacher_cases.append((case, teacher))
    router = fit_factored_cat_branch_router_v1(
        teacher_cases, min_distinct_seeds=min_distinct_seeds,
        item_database=item_database,
    )
    seeds = sorted({row["matrix"]["seed"] for row in rows})
    return {
        "schema": PROPOSAL_SCHEMA,
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "status": "PROPOSAL_ROUTER_READY_NONVOTING",
        "matrix_cell_count": len(MATRIX_RANKS) * len(MATRIX_STRATA),
        "sample_indices": sorted({row["matrix"]["sample_index"] for row in rows}),
        "training_case_count": len(rows),
        "training_seeds": seeds,
        "training_distinct_seed_count": len(seeds),
        "execution_semantic_contract": deepcopy(
            rows[0]["execution_contract"]["semantic"]
        ),
        "router": router,
        "raw_chronicle_rows_loaded": False,
        "voting_eligible": False,
        "deployment_eligible": False,
    }


def fit_matrix_sparse_guard_shortlist_v2(
    training_artifacts: Iterable[Mapping[str, Any]], *,
    item_database: Mapping[str, Any], min_distinct_seeds: int = 6,
    min_opportunity_seeds: int = 6,
) -> dict[str, Any]:
    """Fit a nonvoting sparse-guard shortlist from one balanced training matrix."""

    rows = _validated_phase_artifacts(training_artifacts, "training")
    teacher_cases = []
    for artifact in rows:
        case, _ = _projected_case_and_route(artifact, item_database)
        teacher = artifact["result"]
        if teacher.get("schema") != "cat_external_press_action_teacher/v1":
            raise ValueError("training artifact is not an external-press teacher")
        teacher_cases.append((case, teacher))
    learner = fit_factored_sparse_guard_learner_v2(
        teacher_cases,
        min_distinct_seeds=min_distinct_seeds,
        min_opportunity_seeds=min_opportunity_seeds,
        item_database=item_database,
    )
    seeds = sorted({row["matrix"]["seed"] for row in rows})
    return {
        "schema": SPARSE_SHORTLIST_SCHEMA,
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "status": (
            "SPARSE_GUARD_SHORTLIST_READY_NONVOTING"
            if learner["shortlisted_guard_count"]
            else "ABSTAIN_NO_STABLE_REACHABLE_SPARSE_GUARD"
        ),
        "matrix_cell_count": len(MATRIX_RANKS) * len(MATRIX_STRATA),
        "sample_indices": sorted({row["matrix"]["sample_index"] for row in rows}),
        "training_case_count": len(rows),
        "training_seeds": seeds,
        "training_distinct_seed_count": len(seeds),
        "execution_semantic_contract": deepcopy(
            rows[0]["execution_contract"]["semantic"]
        ),
        "learner": learner,
        "raw_chronicle_rows_loaded": False,
        "full_wave_candidate_policy_evaluated": False,
        "voting_eligible": False,
        "deployment_eligible": False,
    }


def authorize_matrix_transfer_v1(
    proposal_artifact: Mapping[str, Any],
    transfer_artifacts: Iterable[Mapping[str, Any]], *,
    item_database: Mapping[str, Any], min_distinct_seeds: int = 8,
) -> dict[str, Any]:
    if proposal_artifact.get("schema") != PROPOSAL_SCHEMA:
        raise ValueError("proposal artifact schema differs")
    router = proposal_artifact.get("router")
    if not isinstance(router, Mapping):
        raise ValueError("proposal artifact lacks router")
    rows = _validated_phase_artifacts(transfer_artifacts, "held_out_transfer")
    proposal_semantic = proposal_artifact.get("execution_semantic_contract")
    if (
        not isinstance(proposal_semantic, Mapping)
        or dict(proposal_semantic) != rows[0]["execution_contract"]["semantic"]
    ):
        raise ValueError("transfer execution contract differs from training proposal")
    pairs = []
    for artifact in rows:
        case, route = _projected_case_and_route(artifact, item_database)
        selected_route, rule = proposed_rule_for_case_v1(
            router, case, item_database=item_database,
        )
        pair = artifact["result"]
        if (
            pair.get("schema") != "factored_external_press_fresh_pair/v1"
            or pair.get("mechanism_route") != route
            or route != asdict(selected_route)
            or pair.get("rule") != asdict(rule)
        ):
            raise ValueError("transfer pair differs from its frozen proposal")
        pairs.append(pair)
    training_seeds = set(proposal_artifact.get("training_seeds") or [])
    if training_seeds != set(router.get("training_seeds") or []):
        raise ValueError("proposal training seed provenance differs from router")
    transfer_seeds = {row["matrix"]["seed"] for row in rows}
    if training_seeds & transfer_seeds:
        raise ValueError("training and transfer seed namespaces overlap")
    authorized = authorize_factored_routes_v1(
        router, pairs, min_distinct_seeds=min_distinct_seeds,
    )
    return {
        "schema": AUTHORIZATION_SCHEMA,
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "status": "TRANSFER_AUTHORIZATION_COMPLETE_NONVOTING",
        "matrix_cell_count": len(MATRIX_RANKS) * len(MATRIX_STRATA),
        "sample_indices": sorted({row["matrix"]["sample_index"] for row in rows}),
        "training_seeds": sorted(training_seeds),
        "transfer_seeds": sorted(transfer_seeds),
        "transfer_case_count": len(rows),
        "execution_semantic_contract": deepcopy(dict(proposal_semantic)),
        "router": authorized,
        "authorized_route_count": authorized["eligible_fresh_test_route_count"],
        "raw_chronicle_rows_loaded": False,
        "voting_eligible": False,
        "deployment_eligible": False,
    }


def reduce_matrix_fresh_v1(
    authorization_artifact: Mapping[str, Any],
    fresh_artifacts: Iterable[Mapping[str, Any]], *,
    item_database: Mapping[str, Any],
) -> dict[str, Any]:
    if authorization_artifact.get("schema") != AUTHORIZATION_SCHEMA:
        raise ValueError("authorization artifact schema differs")
    router = authorization_artifact.get("router")
    if not isinstance(router, Mapping) or router.get("transfer_gate_status") != "COMPLETE":
        raise ValueError("authorization artifact lacks a completed router")
    rows = _validated_phase_artifacts(fresh_artifacts, "untouched_fresh")
    authorized_semantic = authorization_artifact.get("execution_semantic_contract")
    if (
        not isinstance(authorized_semantic, Mapping)
        or dict(authorized_semantic) != rows[0]["execution_contract"]["semantic"]
    ):
        raise ValueError("fresh execution contract differs from transfer authorization")
    if set(authorization_artifact.get("training_seeds") or []) != set(
        router.get("training_seeds") or []
    ):
        raise ValueError("authorization training seed provenance differs from router")
    summaries = []
    pairs = []
    for artifact in rows:
        case, route = _projected_case_and_route(artifact, item_database)
        selected_route, rule = rule_for_case_v1(
            router, case, item_database=item_database,
        )
        pair = artifact["result"]
        if (
            pair.get("schema") != "factored_external_press_fresh_pair/v1"
            or pair.get("mechanism_route") != route
            or route != asdict(selected_route)
            or pair.get("rule") != asdict(rule)
        ):
            raise ValueError("fresh pair differs from its authorized route")
        pairs.append(pair)
        summaries.append({
            **artifact["matrix"],
            "mechanism_route": route,
            "rule_active": pair.get("rule_active") is True,
            "status": pair.get("status"),
            "technical_receipts_ready": pair.get("technical_receipts_ready") is True,
            "comparison_ready": pair.get("comparison_ready") is True,
            "paired_effective_damage_delta": pair.get("paired_effective_damage_delta"),
            "cat_effective_damage": (pair.get("cat_terminal") or {}).get(
                "own_effective_damage"
            ),
            "candidate_effective_damage": (pair.get("candidate_terminal") or {}).get(
                "own_effective_damage"
            ),
        })
    prior = set(authorization_artifact.get("training_seeds") or []) | set(
        authorization_artifact.get("transfer_seeds") or []
    )
    fresh_seeds = {row["matrix"]["seed"] for row in rows}
    if prior & fresh_seeds:
        raise ValueError("untouched fresh seeds overlap an earlier phase")
    receipts_ready = all(pair.get("technical_receipts_ready") is True for pair in pairs)
    active = [pair for pair in pairs if pair.get("rule_active") is True]
    comparison_ready = bool(
        receipts_ready and active
        and all(pair.get("comparison_ready") is True for pair in active)
    )
    deltas = [
        float(pair["paired_effective_damage_delta"])
        for pair in active
        if pair.get("comparison_ready") is True
        and type(pair.get("paired_effective_damage_delta")) in (int, float)
        and math.isfinite(pair["paired_effective_damage_delta"])
    ]
    return {
        "schema": FRESH_SCHEMA,
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "status": (
            "COMPLETE_MATRIX_FRESH_NONVOTING"
            if receipts_ready else "INCOMPLETE_MATRIX_FRESH_NONVOTING"
        ),
        "matrix_cell_count": len(MATRIX_RANKS) * len(MATRIX_STRATA),
        "sample_indices": sorted({row["matrix"]["sample_index"] for row in rows}),
        "fresh_case_count": len(rows),
        "authorized_route_count": router["eligible_fresh_test_route_count"],
        "execution_semantic_contract": deepcopy(dict(authorized_semantic)),
        "active_fresh_pair_count": len(active),
        "active_ready_delta_count": len(deltas),
        "mean_paired_effective_damage_delta": mean(deltas) if deltas else None,
        "all_semantic_terminal_clock_receipts_valid": receipts_ready,
        "comparison_ready": comparison_ready,
        "fresh_pair_summaries": summaries,
        "route_authorizations": deepcopy(router.get("route_authorizations") or []),
        "raw_chronicle_rows_loaded": False,
        "voting_eligible": False,
        "deployment_eligible": False,
        "scientific_run_launched": False,
    }


def matrix_case_filename_v1(
    rank: int, stratum: str, phase: str, sample_index: int,
) -> str:
    matrix_seed_v1(rank, stratum, phase, sample_index)
    return f"rank{rank}-{stratum}-{phase}-s{sample_index:05d}.json"


def _existing_matrix_artifact_valid(
    path: Path, *, rank: int, stratum: str, phase: str, sample_index: int,
    expected_case: DevelopmentWaveCaseV1,
    expected_execution_contract: Mapping[str, Any],
) -> bool:
    if not path.is_file():
        return False
    try:
        artifact = _read_json(path)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return False
    expected_seed = matrix_seed_v1(rank, stratum, phase, sample_index)
    matrix = artifact.get("matrix")
    projection = artifact.get("case_projection")
    result = artifact.get("result")
    execution_contract = artifact.get("execution_contract")
    if (
        artifact.get("schema") != SCHEMA
        or artifact.get("status") != "COMPLETE_MATRIX_CASE_NONVOTING"
        or not isinstance(matrix, Mapping)
        or not isinstance(projection, Mapping)
        or not isinstance(result, Mapping)
        or not isinstance(execution_contract, Mapping)
        or matrix.get("rank") != rank
        or matrix.get("stratum") != stratum
        or matrix.get("phase") != phase
        or matrix.get("sample_index") != sample_index
        or matrix.get("seed") != expected_seed
        or projection.get("seed") != expected_seed
        or result.get("seed") != expected_seed
        or projection.get("request_sha256") != expected_case.dynamic_load.request_sha256
        or projection.get("dynamic_load_contract_sha256")
        != expected_case.dynamic_load.contract_sha256
        or projection.get("request_sha256") != result.get("request_sha256")
        or projection.get("dynamic_load_contract_sha256")
        != result.get("dynamic_load_contract_sha256")
        or dict(execution_contract) != dict(expected_execution_contract)
    ):
        return False
    expected_result_schema = (
        "cat_external_press_action_teacher/v1"
        if phase == "training" else "factored_external_press_fresh_pair/v1"
    )
    if result.get("schema") != expected_result_schema:
        return False
    try:
        semantic = _execution_semantic(execution_contract)
    except ValueError:
        return False
    if (
        phase == "training"
        and result.get("state_selection_contract")
        != semantic.get("teacher_state_selection_contract")
    ):
        return False
    if result.get("period_ms") != semantic.get("period_ms"):
        return False
    modes = _result_clock_modes(result, phase)
    return bool(
        isinstance(modes, Mapping) and modes
        and all(
            value == REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE
            for value in modes.values()
        )
    )


def _phase_router_wrapper(
    phase: str, router_path: Path | None,
) -> tuple[dict[str, Any] | None, Mapping[str, Any] | None]:
    if phase == "training":
        if router_path is not None:
            raise ValueError("training batch must not receive a router")
        return None, None
    if router_path is None:
        raise ValueError(f"{phase} batch requires a router artifact")
    wrapper = _read_json(router_path)
    expected = (
        PROPOSAL_SCHEMA if phase == "held_out_transfer" else AUTHORIZATION_SCHEMA
    )
    if wrapper.get("schema") != expected or not isinstance(wrapper.get("router"), Mapping):
        raise ValueError("batch router artifact does not match its phase")
    return wrapper, wrapper["router"]


def _batch_initializer(config: Mapping[str, Any]) -> None:
    global _BATCH_CONTEXT
    phase = config["phase"]
    _, router = _phase_router_wrapper(
        phase,
        Path(config["router_path"]) if config.get("router_path") else None,
    )
    _BATCH_CONTEXT = {
        **dict(config),
        "item_database": _load_item_database(Path(config["item_database_path"])),
        "router": router,
    }


def _batch_execute_one(item: tuple[int, str, int, str]) -> dict[str, Any]:
    if _BATCH_CONTEXT is None:
        raise RuntimeError("matrix batch worker was not initialized")
    rank, stratum, sample_index, output_text = item
    config = _BATCH_CONTEXT
    phase = config["phase"]
    path = Path(output_text)
    case = build_matrix_case_v1(
        rank, stratum, phase, sample_index,
        item_database_path=Path(config["item_database_path"]),
        selector_manifest_path=Path(config["selector_manifest_path"]),
        representatives_path=(
            Path(config["representatives_path"])
            if config.get("representatives_path") else None
        ),
        catalog_manifest_path=(
            Path(config["catalog_manifest_path"])
            if config.get("catalog_manifest_path") else None
        ),
        catalog_data_path=(
            Path(config["catalog_data_path"])
            if config.get("catalog_data_path") else None
        ),
    )
    execution_contract = _execution_contract_v1(
        phase=phase, case=case, item_database=config["item_database"],
        router=config["router"], period_ms=config["period_ms"],
        max_states=config["max_states"], max_presses=config["max_presses"],
        bridge_path=Path(config["bridge_path"]),
        bridge_cwd=Path(config["bridge_cwd"]),
        item_database_path=Path(config["item_database_path"]),
        selector_manifest_path=Path(config["selector_manifest_path"]),
        representatives_path=(
            Path(config["representatives_path"])
            if config.get("representatives_path") else None
        ),
        catalog_manifest_path=(
            Path(config["catalog_manifest_path"])
            if config.get("catalog_manifest_path") else None
        ),
        catalog_data_path=(
            Path(config["catalog_data_path"])
            if config.get("catalog_data_path") else None
        ),
        router_path=(
            Path(config["router_path"]) if config.get("router_path") else None
        ),
    )
    artifact = execute_matrix_case_v1(
        case, rank=rank, stratum=stratum, phase=phase,
        sample_index=sample_index,
        bridge_factory=lambda: V14ProjectedDynamicV3Bridge(
            Path(config["bridge_path"]), cwd=Path(config["bridge_cwd"]),
        ),
        item_database=config["item_database"], router=config["router"],
        period_ms=config["period_ms"], max_states=config["max_states"],
        max_presses=config["max_presses"],
        execution_contract=execution_contract,
    )
    _write_json(path, artifact)
    return {
        "rank": rank,
        "stratum": stratum,
        "sample_index": sample_index,
        "seed": matrix_seed_v1(rank, stratum, phase, sample_index),
        "output": str(path),
        "result_status": artifact["result"].get("status"),
    }


def run_matrix_batch_v1(
    *, phase: str, samples_per_cell: int, shard_index: int, shard_count: int,
    workers: int, output_directory: Path, bridge_path: Path = DEFAULT_BRIDGE,
    bridge_cwd: Path = DEFAULT_BRIDGE_CWD,
    item_database_path: Path = DEFAULT_ITEM_DATABASE,
    selector_manifest_path: Path = DEFAULT_SELECTOR_MANIFEST,
    representatives_path: Path | None = None,
    catalog_manifest_path: Path | None = None,
    catalog_data_path: Path | None = None,
    router_path: Path | None = None,
    period_ms: int = 100, max_states: int = 1, max_presses: int = 400,
    sample_start: int = 0,
) -> dict[str, Any]:
    """Run one recoverable scheduler shard over global matrix item ordinals."""

    if phase not in MATRIX_PHASES:
        raise ValueError(f"phase must be one of {MATRIX_PHASES}")
    if type(samples_per_cell) is not int or samples_per_cell < 1:
        raise ValueError("samples_per_cell is outside the matrix seed namespace")
    if type(sample_start) is not int or sample_start < 0:
        raise ValueError("sample_start is outside the matrix seed namespace")
    if sample_start + samples_per_cell - 1 > MAX_SAMPLE_INDEX:
        raise ValueError("sample range is outside the matrix seed namespace")
    if type(shard_count) is not int or shard_count < 1:
        raise ValueError("shard_count must be positive")
    if type(shard_index) is not int or not 0 <= shard_index < shard_count:
        raise ValueError("shard_index must be in [0, shard_count)")
    if type(workers) is not int or workers < 1:
        raise ValueError("workers must be positive")
    if phase == "training" and router_path is not None:
        raise ValueError("training batch must not receive a router")
    if phase != "training" and router_path is None:
        raise ValueError(f"{phase} batch requires a router artifact")
    output_directory.mkdir(parents=True, exist_ok=True)
    config = {
        "phase": phase,
        "bridge_path": str(bridge_path),
        "bridge_cwd": str(bridge_cwd),
        "item_database_path": str(item_database_path),
        "selector_manifest_path": str(selector_manifest_path),
        "representatives_path": str(representatives_path) if representatives_path else None,
        "catalog_manifest_path": (
            str(catalog_manifest_path) if catalog_manifest_path else None
        ),
        "catalog_data_path": str(catalog_data_path) if catalog_data_path else None,
        "router_path": str(router_path) if router_path else None,
        "period_ms": period_ms,
        "max_states": max_states,
        "max_presses": max_presses,
    }
    item_database = _load_item_database(item_database_path)
    router_wrapper, router = _phase_router_wrapper(phase, router_path)
    global_items = [
        (rank, stratum, sample_index)
        for rank in MATRIX_RANKS
        for stratum in MATRIX_STRATA
        for sample_index in range(sample_start, sample_start + samples_per_cell)
    ]
    assigned = [
        item for ordinal, item in enumerate(global_items)
        if ordinal % shard_count == shard_index
    ]
    if router_wrapper is not None:
        probe_rank, probe_stratum, probe_sample_index = (
            assigned[0] if assigned else global_items[0]
        )
        probe_case = build_matrix_case_v1(
            probe_rank, probe_stratum, phase, probe_sample_index,
            item_database_path=item_database_path,
            selector_manifest_path=selector_manifest_path,
            representatives_path=representatives_path,
            catalog_manifest_path=catalog_manifest_path,
            catalog_data_path=catalog_data_path,
        )
        probe_contract = _execution_contract_v1(
            phase=phase, case=probe_case, item_database=item_database,
            router=router, period_ms=period_ms, max_states=max_states,
            max_presses=max_presses, bridge_path=bridge_path,
            bridge_cwd=bridge_cwd, item_database_path=item_database_path,
            selector_manifest_path=selector_manifest_path,
            representatives_path=representatives_path,
            catalog_manifest_path=catalog_manifest_path,
            catalog_data_path=catalog_data_path, router_path=router_path,
        )
        if router_wrapper.get("execution_semantic_contract") != (
            probe_contract["semantic"]
        ):
            raise ValueError("batch execution contract differs from preceding phase")
    skipped = []
    pending = []
    for rank, stratum, sample_index in assigned:
        path = output_directory / matrix_case_filename_v1(
            rank, stratum, phase, sample_index,
        )
        row = (rank, stratum, sample_index, str(path))
        if not path.is_file():
            pending.append(row)
            continue
        expected_case = build_matrix_case_v1(
            rank, stratum, phase, sample_index,
            item_database_path=item_database_path,
            selector_manifest_path=selector_manifest_path,
            representatives_path=representatives_path,
            catalog_manifest_path=catalog_manifest_path,
            catalog_data_path=catalog_data_path,
        )
        expected_execution_contract = _execution_contract_v1(
            phase=phase, case=expected_case, item_database=item_database,
            router=router, period_ms=period_ms, max_states=max_states,
            max_presses=max_presses, bridge_path=bridge_path,
            bridge_cwd=bridge_cwd, item_database_path=item_database_path,
            selector_manifest_path=selector_manifest_path,
            representatives_path=representatives_path,
            catalog_manifest_path=catalog_manifest_path,
            catalog_data_path=catalog_data_path, router_path=router_path,
        )
        if _existing_matrix_artifact_valid(
            path, rank=rank, stratum=stratum, phase=phase,
            sample_index=sample_index,
            expected_case=expected_case,
            expected_execution_contract=expected_execution_contract,
        ):
            skipped.append(row)
        else:
            pending.append(row)
    completed: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    if pending:
        with ProcessPoolExecutor(
            max_workers=min(workers, len(pending)),
            initializer=_batch_initializer,
            initargs=(config,),
        ) as executor:
            futures = {
                executor.submit(_batch_execute_one, row): row for row in pending
            }
            for future in as_completed(futures):
                rank, stratum, sample_index, path = futures[future]
                try:
                    completed.append(future.result())
                except Exception as error:
                    failures.append({
                        "rank": rank,
                        "stratum": stratum,
                        "sample_index": sample_index,
                        "seed": matrix_seed_v1(
                            rank, stratum, phase, sample_index,
                        ),
                        "output": path,
                        "error": f"{type(error).__name__}: {error}",
                    })
    completed.sort(key=lambda row: (
        MATRIX_RANKS.index(row["rank"]), MATRIX_STRATA.index(row["stratum"]),
        row["sample_index"],
    ))
    failures.sort(key=lambda row: (
        MATRIX_RANKS.index(row["rank"]), MATRIX_STRATA.index(row["stratum"]),
        row["sample_index"],
    ))
    return {
        "schema": BATCH_SCHEMA,
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "status": "COMPLETE_BATCH_SHARD" if not failures else "INCOMPLETE_BATCH_SHARD",
        "phase": phase,
        "sample_start": sample_start,
        "samples_per_cell": samples_per_cell,
        "global_item_count": len(global_items),
        "shard_index": shard_index,
        "shard_count": shard_count,
        "assigned_item_count": len(assigned),
        "skipped_existing_count": len(skipped),
        "executed_item_count": len(completed),
        "failed_item_count": len(failures),
        "workers": min(workers, len(pending)) if pending else 0,
        "assignment_contract": "GLOBAL_ITEM_ORDINAL_MOD_SHARD_COUNT",
        "completed": completed,
        "failures": failures,
        "raw_chronicle_rows_loaded": False,
        "voting_eligible": False,
        "deployment_eligible": False,
    }


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON artifact must be an object: {path}")
    return value


def _read_inputs(paths: Iterable[Path]) -> list[dict[str, Any]]:
    return [_read_json(path) for path in paths]


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _add_item_database(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--item-db", type=Path, default=DEFAULT_ITEM_DATABASE)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    worker = commands.add_parser("worker", help="run one rank/stratum/phase/seed cell")
    worker.add_argument("--rank", type=int, choices=MATRIX_RANKS, required=True)
    worker.add_argument("--stratum", choices=MATRIX_STRATA, required=True)
    worker.add_argument("--phase", choices=MATRIX_PHASES, required=True)
    worker.add_argument("--sample-index", type=int, required=True)
    worker.add_argument("--bridge", type=Path, default=DEFAULT_BRIDGE)
    worker.add_argument("--bridge-cwd", type=Path, default=DEFAULT_BRIDGE_CWD)
    worker.add_argument("--router", type=Path)
    worker.add_argument("--selector-manifest", type=Path, default=DEFAULT_SELECTOR_MANIFEST)
    worker.add_argument("--representatives", type=Path)
    worker.add_argument("--catalog-manifest", type=Path)
    worker.add_argument("--catalog-data", type=Path)
    worker.add_argument("--period-ms", type=int, default=100)
    worker.add_argument("--max-states", type=int, default=1)
    worker.add_argument("--max-presses", type=int, default=400)
    worker.add_argument("--output", type=Path, required=True)
    _add_item_database(worker)

    batch = commands.add_parser(
        "batch", help="run one recoverable modulo-assigned scheduler shard"
    )
    batch.add_argument("--phase", choices=MATRIX_PHASES, required=True)
    batch.add_argument("--sample-start", type=int, default=0)
    batch.add_argument("--samples-per-cell", type=int, required=True)
    batch.add_argument("--shard-index", type=int, required=True)
    batch.add_argument("--shard-count", type=int, required=True)
    batch.add_argument("--workers", type=int, required=True)
    batch.add_argument("--bridge", type=Path, default=DEFAULT_BRIDGE)
    batch.add_argument("--bridge-cwd", type=Path, default=DEFAULT_BRIDGE_CWD)
    batch.add_argument("--router", type=Path)
    batch.add_argument("--selector-manifest", type=Path, default=DEFAULT_SELECTOR_MANIFEST)
    batch.add_argument("--representatives", type=Path)
    batch.add_argument("--catalog-manifest", type=Path)
    batch.add_argument("--catalog-data", type=Path)
    batch.add_argument("--period-ms", type=int, default=100)
    batch.add_argument("--max-states", type=int, default=1)
    batch.add_argument("--max-presses", type=int, default=400)
    batch.add_argument("--output-dir", type=Path, required=True)
    batch.add_argument("--summary", type=Path, required=True)
    _add_item_database(batch)

    fit = commands.add_parser("fit", help="fit proposal router from training artifacts")
    fit.add_argument("--input", type=Path, nargs="+", required=True)
    fit.add_argument("--min-distinct-seeds", type=int, default=6)
    fit.add_argument("--output", type=Path, required=True)
    _add_item_database(fit)

    fit_sparse = commands.add_parser(
        "fit-sparse", help="fit the seed-blocked sparse-guard development shortlist"
    )
    fit_sparse.add_argument("--input", type=Path, nargs="+", required=True)
    fit_sparse.add_argument("--min-distinct-seeds", type=int, default=6)
    fit_sparse.add_argument("--min-opportunity-seeds", type=int, default=6)
    fit_sparse.add_argument("--output", type=Path, required=True)
    _add_item_database(fit_sparse)

    authorize = commands.add_parser(
        "authorize", help="authorize exact routes from held-out transfer artifacts"
    )
    authorize.add_argument("--proposal", type=Path, required=True)
    authorize.add_argument("--input", type=Path, nargs="+", required=True)
    authorize.add_argument("--min-distinct-seeds", type=int, default=8)
    authorize.add_argument("--output", type=Path, required=True)
    _add_item_database(authorize)

    fresh = commands.add_parser("fresh", help="reduce untouched fresh artifacts")
    fresh.add_argument("--authorization", type=Path, required=True)
    fresh.add_argument("--input", type=Path, nargs="+", required=True)
    fresh.add_argument("--output", type=Path, required=True)
    _add_item_database(fresh)

    args = parser.parse_args()
    if args.command == "batch":
        result = run_matrix_batch_v1(
            phase=args.phase, samples_per_cell=args.samples_per_cell,
            sample_start=args.sample_start,
            shard_index=args.shard_index, shard_count=args.shard_count,
            workers=args.workers, output_directory=args.output_dir,
            bridge_path=args.bridge, bridge_cwd=args.bridge_cwd,
            item_database_path=args.item_db,
            selector_manifest_path=args.selector_manifest,
            representatives_path=args.representatives,
            catalog_manifest_path=args.catalog_manifest,
            catalog_data_path=args.catalog_data, router_path=args.router,
            period_ms=args.period_ms, max_states=args.max_states,
            max_presses=args.max_presses,
        )
        _write_json(args.summary, result)
        if result["failed_item_count"]:
            raise SystemExit(1)
        return

    item_database = _load_item_database(args.item_db)
    if args.command == "worker":
        router_artifact = _read_json(args.router) if args.router else None
        if args.phase == "training":
            router = None
        elif args.phase == "held_out_transfer":
            if router_artifact is None or router_artifact.get("schema") != PROPOSAL_SCHEMA:
                parser.error("held_out_transfer requires --router from the fit command")
            router = router_artifact["router"]
        else:
            if router_artifact is None or router_artifact.get("schema") != AUTHORIZATION_SCHEMA:
                parser.error("untouched_fresh requires --router from the authorize command")
            router = router_artifact["router"]
        case = build_matrix_case_v1(
            args.rank, args.stratum, args.phase, args.sample_index,
            item_database_path=args.item_db,
            selector_manifest_path=args.selector_manifest,
            representatives_path=args.representatives,
            catalog_manifest_path=args.catalog_manifest,
            catalog_data_path=args.catalog_data,
        )
        execution_contract = _execution_contract_v1(
            phase=args.phase, case=case, item_database=item_database,
            router=router, period_ms=args.period_ms, max_states=args.max_states,
            max_presses=args.max_presses, bridge_path=args.bridge,
            bridge_cwd=args.bridge_cwd, item_database_path=args.item_db,
            selector_manifest_path=args.selector_manifest,
            representatives_path=args.representatives,
            catalog_manifest_path=args.catalog_manifest,
            catalog_data_path=args.catalog_data, router_path=args.router,
        )
        if (
            router_artifact is not None
            and router_artifact.get("execution_semantic_contract")
            != execution_contract["semantic"]
        ):
            parser.error("worker execution contract differs from preceding phase")
        result = execute_matrix_case_v1(
            case, rank=args.rank, stratum=args.stratum, phase=args.phase,
            sample_index=args.sample_index,
            bridge_factory=lambda: V14ProjectedDynamicV3Bridge(
                args.bridge, cwd=args.bridge_cwd,
            ),
            item_database=item_database, router=router,
            period_ms=args.period_ms, max_states=args.max_states,
            max_presses=args.max_presses,
            execution_contract=execution_contract,
        )
    elif args.command == "fit":
        result = fit_matrix_proposal_v1(
            _read_inputs(args.input), item_database=item_database,
            min_distinct_seeds=args.min_distinct_seeds,
        )
    elif args.command == "fit-sparse":
        result = fit_matrix_sparse_guard_shortlist_v2(
            _read_inputs(args.input), item_database=item_database,
            min_distinct_seeds=args.min_distinct_seeds,
            min_opportunity_seeds=args.min_opportunity_seeds,
        )
    elif args.command == "authorize":
        result = authorize_matrix_transfer_v1(
            _read_json(args.proposal), _read_inputs(args.input),
            item_database=item_database,
            min_distinct_seeds=args.min_distinct_seeds,
        )
    else:
        result = reduce_matrix_fresh_v1(
            _read_json(args.authorization), _read_inputs(args.input),
            item_database=item_database,
        )
    _write_json(args.output, result)


if __name__ == "__main__":
    main()


__all__ = (
    "MATRIX_RANKS", "MATRIX_STRATA", "MATRIX_PHASES", "STRATUM_BINDINGS",
    "matrix_seed_v1", "build_matrix_case_v1", "execute_matrix_case_v1",
    "build_matrix_case_from_seed_v1",
    "run_matrix_case_v1", "fit_matrix_proposal_v1",
    "fit_matrix_sparse_guard_shortlist_v2",
    "authorize_matrix_transfer_v1", "reduce_matrix_fresh_v1",
    "matrix_case_filename_v1", "run_matrix_batch_v1",
)
