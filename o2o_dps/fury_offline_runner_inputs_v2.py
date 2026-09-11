"""Verify a V2 offline corpus and materialize paired-runner scenario inputs.

This is a read-only integration boundary.  Every selected corpus entry is
resolved back to its compressed source catalog and verified before its exact
simulator request can enter a runner plan.  No simulator process is launched.
"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from o2o_dps.fury_offline_corpus_v2 import sha256_json, verify_content_address
from o2o_dps.fury_paired_multiseed_runner_v2 import (
    SCENARIO_MODEL_KIND,
    TARGET_CONTEXT_BUNDLE_KIND,
    runner_scenario_bundle_sha256,
    runner_scenario_model_bundle_sha256,
    runner_target_context_bundle_set_sha256,
)


JSONMap = dict[str, Any]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 2
IMPLEMENTATION_REVISION = "v2.1_unbound_dynamic_scenario_semantics"
ALLOWED_BUCKETS = frozenset(
    ("main_comparison", "unknown_layout_sensitivity", "short_auxiliary")
)
MAIN_VARIANTS = frozenset(("single_target", "cohit_stacked"))
MAIN_LAYOUT_SIDE = "evidence_bounded"
MIN_MAIN_HORIZON_MS = 5_000
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")


class FuryOfflineRunnerInputsError(RuntimeError):
    """A corpus or catalog failed the runner-input provenance contract."""


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FuryOfflineRunnerInputsError(f"{label} must be a JSON object")
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise FuryOfflineRunnerInputsError(f"{label} must be a JSON array")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FuryOfflineRunnerInputsError(f"{label} must be a non-empty string")
    return value.strip()


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FuryOfflineRunnerInputsError(f"{label} must be an integer")
    return value


def _lower_sha256(value: Any, label: str) -> str:
    rendered = _text(value, label)
    if not _SHA256.fullmatch(rendered):
        raise FuryOfflineRunnerInputsError(
            f"{label} must be a lowercase 64-character SHA-256"
        )
    return rendered


def _load_json(path: Path) -> JSONMap:
    try:
        if path.suffix.casefold() == ".gz":
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                value = json.load(handle)
        else:
            value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise FuryOfflineRunnerInputsError(f"cannot read JSON artifact {path}: {error}") from error
    if not isinstance(value, dict):
        raise FuryOfflineRunnerInputsError(f"JSON artifact is not an object: {path}")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise FuryOfflineRunnerInputsError(f"cannot hash catalog {path}: {error}") from error
    return digest.hexdigest()


def _resolve_catalog(project_root: Path, raw_path: Any) -> Path:
    rendered = _text(raw_path, "catalog_locator.path")
    relative = Path(rendered)
    if relative.is_absolute():
        raise FuryOfflineRunnerInputsError(
            "catalog_locator.path must be relative to the declared project root"
        )
    root = project_root.expanduser().resolve()
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise FuryOfflineRunnerInputsError(
            f"catalog locator escapes the project root: {rendered}"
        ) from error
    if not resolved.is_file():
        raise FuryOfflineRunnerInputsError(f"catalog locator does not exist: {resolved}")
    return resolved


def load_verified_corpus_manifest(path: Path) -> JSONMap:
    """Load and verify the canonical content address of a corpus manifest."""

    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FuryOfflineRunnerInputsError(f"corpus manifest does not exist: {resolved}")
    manifest = _load_json(resolved)
    if (
        manifest.get("schema_version") != 2
        or manifest.get("schema") != "fury_offline_corpus/v2"
        or manifest.get("kind") != "fury_offline_corpus_manifest_v2"
    ):
        raise FuryOfflineRunnerInputsError("invalid fury_offline_corpus/v2 schema or kind")
    try:
        valid = verify_content_address(manifest)
    except Exception as error:
        raise FuryOfflineRunnerInputsError(
            f"cannot verify corpus content address: {error}"
        ) from error
    if not valid:
        raise FuryOfflineRunnerInputsError("corpus manifest content address mismatch")
    address = _mapping(manifest.get("content_address"), "manifest.content_address")
    _lower_sha256(address.get("sha256"), "manifest.content_address.sha256")
    return manifest


def _catalog_registry(manifest: Mapping[str, Any]) -> dict[str, JSONMap]:
    inputs = _mapping(manifest.get("inputs"), "manifest.inputs")
    catalogs = _array(inputs.get("catalogs"), "manifest.inputs.catalogs")
    registry: dict[str, JSONMap] = {}
    for index, raw_catalog in enumerate(catalogs):
        catalog = _mapping(raw_catalog, f"manifest.inputs.catalogs[{index}]")
        path = _text(catalog.get("path"), f"manifest.inputs.catalogs[{index}].path")
        if path in registry:
            raise FuryOfflineRunnerInputsError(f"duplicate catalog registry path: {path}")
        registry[path] = {
            "sha256": _lower_sha256(
                catalog.get("sha256"), f"manifest.inputs.catalogs[{index}].sha256"
            ),
            "size_bytes": _integer(
                catalog.get("size_bytes"),
                f"manifest.inputs.catalogs[{index}].size_bytes",
            ),
            "scenario_count": _integer(
                catalog.get("scenario_count"),
                f"manifest.inputs.catalogs[{index}].scenario_count",
            ),
        }
    if not registry:
        raise FuryOfflineRunnerInputsError("corpus manifest contains no catalog registry")
    return registry


def _classify_source_scenario(source: Mapping[str, Any]) -> tuple[str, int, str, str]:
    duration = _mapping(source.get("duration"), "source_scenario.duration")
    horizon = _integer(
        duration.get("observed_span_ms"),
        "source_scenario.duration.observed_span_ms",
    )
    if horizon < 0:
        raise FuryOfflineRunnerInputsError("source scenario horizon cannot be negative")
    pile = _mapping(source.get("pile"), "source_scenario.pile")
    variant = _text(pile.get("layout_variant"), "source_scenario.pile.layout_variant")
    layout_side = _text(pile.get("layout_side"), "source_scenario.pile.layout_side")
    if horizon < MIN_MAIN_HORIZON_MS:
        return "short_auxiliary", horizon, variant, layout_side
    if variant in MAIN_VARIANTS and layout_side == MAIN_LAYOUT_SIDE:
        return "main_comparison", horizon, variant, layout_side
    return "unknown_layout_sensitivity", horizon, variant, layout_side


def _verify_entry_copies(entry: Mapping[str, Any], source: Mapping[str, Any]) -> None:
    pairs = (
        ("source", "source"),
        ("pile", "pile"),
        ("target_hypotheses", "target_hypotheses"),
        ("kill_budget_proxies", "kill_budget_proxies"),
        ("weight", "weight"),
    )
    for entry_field, source_field in pairs:
        if entry.get(entry_field) != source.get(source_field):
            raise FuryOfflineRunnerInputsError(
                f"corpus entry {entry_field} does not match source catalog scenario"
            )
    duration = _mapping(source.get("duration"), "source_scenario.duration")
    if entry.get("observed_span_ms") != duration.get("observed_span_ms"):
        raise FuryOfflineRunnerInputsError(
            "corpus entry observed_span_ms does not match source catalog scenario"
        )
    pile = _mapping(source.get("pile"), "source_scenario.pile")
    if entry.get("layout_variant") != pile.get("layout_variant"):
        raise FuryOfflineRunnerInputsError(
            "corpus entry layout_variant does not match source catalog scenario"
        )
    if entry.get("layout_side") != pile.get("layout_side"):
        raise FuryOfflineRunnerInputsError(
            "corpus entry layout_side does not match source catalog scenario"
        )


def _verify_declared_hashes(
    entry: Mapping[str, Any], source_scenario: Mapping[str, Any]
) -> JSONMap:
    hashes = _mapping(entry.get("hashes"), "corpus_entry.hashes")
    expected_values = {
        "scenario_sha256": source_scenario,
        "source_sha256": _mapping(source_scenario.get("source"), "source_scenario.source"),
        "request_sha256": _mapping(source_scenario.get("request"), "source_scenario.request"),
        "pile_sha256": _mapping(source_scenario.get("pile"), "source_scenario.pile"),
        "target_hypotheses_sha256": _mapping(
            source_scenario.get("target_hypotheses"),
            "source_scenario.target_hypotheses",
        ),
        "kill_budget_proxies_sha256": _array(
            source_scenario.get("kill_budget_proxies"),
            "source_scenario.kill_budget_proxies",
        ),
        "weight_sha256": _mapping(source_scenario.get("weight"), "source_scenario.weight"),
    }
    verified: JSONMap = {}
    for field, value in expected_values.items():
        declared = _lower_sha256(hashes.get(field), f"corpus_entry.hashes.{field}")
        actual = sha256_json(value)
        if declared != actual:
            raise FuryOfflineRunnerInputsError(
                f"corpus entry {field} does not match source catalog scenario"
            )
        verified[field] = actual
    return verified


def _strict_request(source_scenario: Mapping[str, Any], horizon_ms: int) -> JSONMap:
    request = _mapping(source_scenario.get("request"), "source_scenario.request")
    try:
        exact = json.loads(
            json.dumps(
                request,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise FuryOfflineRunnerInputsError(f"source request is not strict JSON: {error}") from error
    encounter = _mapping(exact.get("encounter"), "source_request.encounter")
    if encounter.get("useHealth") is not False:
        raise FuryOfflineRunnerInputsError(
            "source request must remain duration mode with encounter.useHealth == false"
        )
    duration = encounter.get("duration")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        raise FuryOfflineRunnerInputsError("source request encounter.duration must be numeric")
    if not math.isfinite(float(duration)) or abs(float(duration) * 1000 - horizon_ms) > 1:
        raise FuryOfflineRunnerInputsError(
            "source request duration does not match the observed scenario horizon"
        )
    return exact


def _verify_catalog(
    path: Path,
    *,
    expected_sha256: str,
    expected_size: int,
    expected_count: int,
) -> JSONMap:
    actual_size = path.stat().st_size
    if actual_size != expected_size:
        raise FuryOfflineRunnerInputsError(
            f"catalog byte size mismatch for {path}: {actual_size} != {expected_size}"
        )
    actual_sha = _sha256_file(path)
    if actual_sha != expected_sha256:
        raise FuryOfflineRunnerInputsError(f"catalog byte SHA-256 mismatch for {path}")
    catalog = _load_json(path)
    if catalog.get("kind") != "fury_encounter_scenario_catalog_v1":
        raise FuryOfflineRunnerInputsError(f"invalid source catalog kind: {path}")
    scenarios = _array(catalog.get("scenarios"), f"{path}.scenarios")
    if len(scenarios) != expected_count:
        raise FuryOfflineRunnerInputsError(
            f"catalog scenario count mismatch for {path}: {len(scenarios)} != {expected_count}"
        )
    return catalog


def materialize_runner_inputs_v2(
    manifest_path: Path,
    *,
    bucket: str = "main_comparison",
    project_root: Path = PROJECT_ROOT,
) -> JSONMap:
    """Return verified scenario objects accepted by the paired V2 runner."""

    if bucket not in ALLOWED_BUCKETS:
        raise FuryOfflineRunnerInputsError(f"unsupported corpus bucket: {bucket}")
    manifest = load_verified_corpus_manifest(manifest_path)
    corpus_sha = _lower_sha256(
        _mapping(manifest.get("content_address"), "manifest.content_address").get("sha256"),
        "manifest.content_address.sha256",
    )
    registry = _catalog_registry(manifest)
    entries = _array(manifest.get("scenarios"), "manifest.scenarios")
    normalized_entries = [
        _mapping(value, f"manifest.scenarios[{index}]")
        for index, value in enumerate(entries)
    ]
    selected = [
        value for value in normalized_entries if value.get("bucket") == bucket
    ]
    if not selected:
        raise FuryOfflineRunnerInputsError(f"corpus bucket is empty: {bucket}")
    summary = _mapping(manifest.get("summary"), "manifest.summary")
    buckets = _mapping(summary.get("buckets"), "manifest.summary.buckets")
    bucket_summary = _mapping(buckets.get(bucket), f"manifest.summary.buckets.{bucket}")
    declared_count = _integer(
        bucket_summary.get("scenario_count"),
        f"manifest.summary.buckets.{bucket}.scenario_count",
    )
    if declared_count != len(selected):
        raise FuryOfflineRunnerInputsError(
            f"selected bucket count {len(selected)} does not match manifest summary {declared_count}"
        )

    loaded_catalogs: dict[str, JSONMap] = {}
    preliminary: list[JSONMap] = []
    scenario_ids: set[str] = set()
    for entry_index, entry in enumerate(selected):
        scenario_id = _text(entry.get("scenario_id"), f"selected[{entry_index}].scenario_id")
        if scenario_id in scenario_ids:
            raise FuryOfflineRunnerInputsError(
                f"duplicate scenario_id in selected bucket: {scenario_id}"
            )
        scenario_ids.add(scenario_id)
        locator = _mapping(entry.get("catalog_locator"), "corpus_entry.catalog_locator")
        locator_path = _text(locator.get("path"), "catalog_locator.path")
        registered = registry.get(locator_path)
        if registered is None:
            raise FuryOfflineRunnerInputsError(
                f"catalog locator is absent from the corpus registry: {locator_path}"
            )
        catalog_sha = _lower_sha256(
            locator.get("catalog_sha256"), "catalog_locator.catalog_sha256"
        )
        if catalog_sha != registered["sha256"]:
            raise FuryOfflineRunnerInputsError(
                f"catalog locator SHA-256 conflicts with registry: {locator_path}"
            )
        catalog_path = _resolve_catalog(project_root, locator_path)
        if locator_path not in loaded_catalogs:
            loaded_catalogs[locator_path] = _verify_catalog(
                catalog_path,
                expected_sha256=catalog_sha,
                expected_size=int(registered["size_bytes"]),
                expected_count=int(registered["scenario_count"]),
            )
        catalog = loaded_catalogs[locator_path]
        source_scenarios = _array(catalog.get("scenarios"), f"{catalog_path}.scenarios")
        source_index = _integer(locator.get("scenario_index"), "catalog_locator.scenario_index")
        if source_index < 0 or source_index >= len(source_scenarios):
            raise FuryOfflineRunnerInputsError(
                f"catalog scenario index is out of range: {source_index}"
            )
        source_scenario = _mapping(
            source_scenarios[source_index],
            f"{catalog_path}.scenarios[{source_index}]",
        )
        if source_scenario.get("scenario_id") != scenario_id:
            raise FuryOfflineRunnerInputsError(
                "catalog scenario ID does not match the corpus entry locator"
            )
        _verify_entry_copies(entry, source_scenario)
        verified_hashes = _verify_declared_hashes(entry, source_scenario)
        actual_bucket, horizon, variant, layout_side = _classify_source_scenario(
            source_scenario
        )
        if actual_bucket != bucket:
            raise FuryOfflineRunnerInputsError(
                f"{bucket} contains {actual_bucket} source scenario {scenario_id}"
            )
        if horizon <= 0:
            raise FuryOfflineRunnerInputsError(
                f"source scenario {scenario_id} has no positive runnable horizon"
            )
        if bucket == "main_comparison":
            if (
                horizon < MIN_MAIN_HORIZON_MS
                or variant not in MAIN_VARIANTS
                or layout_side != MAIN_LAYOUT_SIDE
            ):
                raise FuryOfflineRunnerInputsError(
                    "main comparison must contain only >=5000 ms evidence-bounded "
                    "single_target/cohit scenarios"
                )
        pile = _mapping(source_scenario.get("pile"), "source_scenario.pile")
        target_count = _integer(pile.get("target_count"), "source_scenario.pile.target_count")
        if target_count <= 0:
            raise FuryOfflineRunnerInputsError("source scenario target_count must be positive")
        request = _strict_request(source_scenario, horizon)
        encounter = _mapping(request.get("encounter"), "source_request.encounter")
        request_targets = _array(encounter.get("targets"), "source_request.encounter.targets")
        if len(request_targets) != target_count:
            raise FuryOfflineRunnerInputsError(
                "source request target count does not match scenario pile.target_count"
            )
        if variant == "single_target" and target_count != 1:
            raise FuryOfflineRunnerInputsError("single_target scenario must have one target")
        if variant == "cohit_stacked" and target_count < 2:
            raise FuryOfflineRunnerInputsError("cohit_stacked scenario must have multiple targets")
        source = _mapping(source_scenario.get("source"), "source_scenario.source")
        instance_id = _text(source.get("instance"), "source_scenario.source.instance")
        component = _mapping(entry.get("leakage_component"), "entry.leakage_component")
        component_id = _text(
            component.get("component_id"), "entry.leakage_component.component_id"
        )
        link_status = _text(
            component.get("link_status"), "entry.leakage_component.link_status"
        )
        if bucket == "main_comparison" and link_status != "resolved":
            raise FuryOfflineRunnerInputsError(
                "main comparison requires a resolved leakage-component assignment"
            )
        family_id = _text(pile.get("sensitivity_family"), "pile.sensitivity_family")
        request_sha = sha256_json(request)
        limitation_codes = [
            "BASE_OR_EFFECTIVE_ARMOR_NOT_EXACTLY_BOUND",
            "DYNAMIC_ARMOR_SCHEDULE_UNBOUND",
            "DYNAMIC_ATTACKABILITY_SCHEDULE_UNBOUND",
            "ENDOGENOUS_TTK_BACKGROUND_DAMAGE_UNBOUND",
            "EXACT_TARGET_CONTEXT_UNBOUND",
            "WOW_UNIT_CLASSIFICATION_UNBOUND",
        ]
        target_context_bundle: JSONMap = {
            "schema_version": 2,
            "kind": TARGET_CONTEXT_BUNDLE_KIND,
            "binding_status": "OFFLINE_CATALOG_TARGET_CONTEXT_UNBOUND",
            "request_sha256": request_sha,
            "target_count": target_count,
            "contexts": [
                {
                    "target_index": target_index,
                    "request_target_sha256": sha256_json(request_target),
                    "binding_status": "UNBOUND_DIAGNOSTIC_DESCRIPTOR",
                }
                for target_index, request_target in enumerate(request_targets)
            ],
            "comparison_eligible": False,
            "bridge_execution_eligible": False,
            "limitation_codes": limitation_codes,
        }
        target_context_bundle_sha = sha256_json(target_context_bundle)
        scenario_model: JSONMap = {
            "schema_version": 2,
            "kind": SCENARIO_MODEL_KIND,
            "model_status": "OFFLINE_CATALOG_DYNAMIC_SEMANTICS_UNBOUND",
            "request_sha256": request_sha,
            "target_context_bundle_sha256": target_context_bundle_sha,
            "historical_truth": False,
            "comparison_eligible": False,
            "bridge_execution_eligible": False,
            "dynamic_armor_schedule_status": "UNBOUND_FROM_CATALOG_REQUEST",
            "dynamic_attackability_schedule_status": "UNBOUND_FROM_CATALOG_REQUEST",
            "health_or_horizon_status": (
                "OBSERVED_SPAN_DURATION_PROXY_NOT_EXACT_ATTACKABILITY_OR_DEATH"
            ),
            "dynamic_semantics_receipt": {
                "status": "CATALOG_EVIDENCE_RETAINED_UNBOUND",
                "pile": deepcopy(source_scenario["pile"]),
                "target_hypotheses": deepcopy(source_scenario["target_hypotheses"]),
                "kill_budget_proxies": deepcopy(source_scenario["kill_budget_proxies"]),
            },
            "limitation_codes": limitation_codes,
        }
        preliminary.append(
            {
                "instance_id": instance_id,
                "component_id": component_id,
                "scenario_id": scenario_id,
                "family_id": family_id,
                "stratum": "single_target" if target_count == 1 else "multi_target",
                "horizon_ms": horizon,
                "target_count": target_count,
                "estimated_cost_units": horizon * target_count,
                "request": request,
                "scenario_model": scenario_model,
                "scenario_model_sha256": sha256_json(scenario_model),
                "target_context_bundle": target_context_bundle,
                "target_context_bundle_sha256": target_context_bundle_sha,
                "request_sha256": verified_hashes["request_sha256"],
                "corpus_manifest_sha256": corpus_sha,
                "corpus_entry_sha256": sha256_json(entry),
                "source_scenario_sha256": verified_hashes["scenario_sha256"],
                "catalog_sha256": catalog_sha,
                "source_sha256": verified_hashes["source_sha256"],
                "pile_sha256": verified_hashes["pile_sha256"],
                "target_hypotheses_sha256": verified_hashes[
                    "target_hypotheses_sha256"
                ],
                "kill_budget_proxies_sha256": verified_hashes[
                    "kill_budget_proxies_sha256"
                ],
                "weight_sha256": verified_hashes["weight_sha256"],
            }
        )

    family_counts: Counter[tuple[str, str]] = Counter(
        (str(row["instance_id"]), str(row["family_id"])) for row in preliminary
    )
    materialized: list[JSONMap] = []
    for row in preliminary:
        alternative_count = family_counts[(str(row["instance_id"]), str(row["family_id"]))]
        materialized.append(
            {
                **row,
                "scenario_weight": 1.0 / alternative_count,
                "family_weight_contract": {
                    "total_family_weight": 1,
                    "alternative_count": alternative_count,
                    "per_alternative_numerator": 1,
                    "per_alternative_denominator": alternative_count,
                },
            }
        )
    materialized.sort(key=lambda row: (str(row["instance_id"]), str(row["scenario_id"])))
    family_weight_sums: dict[tuple[str, str], float] = Counter()
    for row in materialized:
        family_weight_sums[(str(row["instance_id"]), str(row["family_id"]))] += float(
            row["scenario_weight"]
        )
    if any(abs(value - 1.0) > 1e-12 for value in family_weight_sums.values()):
        raise FuryOfflineRunnerInputsError("family alternative weights do not sum to one")

    contract: JSONMap = {
        "schema_version": SCHEMA_VERSION,
        "kind": "fury_offline_runner_inputs_v2",
        "implementation_revision": IMPLEMENTATION_REVISION,
        "bucket": bucket,
        "corpus_manifest_sha256": corpus_sha,
        "selection": {
            "catalogs_verified": len(loaded_catalogs),
            "scenario_count": len(materialized),
            "family_count": len(family_counts),
            "stratum_counts": dict(
                sorted(Counter(str(row["stratum"]) for row in materialized).items())
            ),
            "total_estimated_cost_units": sum(
                int(row["estimated_cost_units"]) for row in materialized
            ),
            "equal_total_weight_per_family": True,
            "scenario_models_content_addressed": True,
            "target_context_bundles_content_addressed": True,
            "comparison_eligible_scenario_count": 0,
            "bridge_execution_eligible_scenario_count": 0,
            "dynamic_armor_schedules_bound": False,
            "dynamic_attackability_schedules_bound": False,
            "endogenous_ttk_bound": False,
            "simulator_execution_started": False,
        },
        "scenarios": materialized,
    }
    return {
        **contract,
        "runner_inputs_sha256": sha256_json(contract),
        "runner_scenario_bundle_sha256": runner_scenario_bundle_sha256(
            materialized
        ),
        "scenario_model_bundle_sha256": runner_scenario_model_bundle_sha256(
            materialized
        ),
        "target_context_bundle_set_sha256": runner_target_context_bundle_set_sha256(
            materialized
        ),
    }


def _plan_document(result: Mapping[str, Any]) -> JSONMap:
    return {
        "kind": "fury_offline_runner_inputs_plan_v2",
        "bucket": result.get("bucket"),
        "corpus_manifest_sha256": result.get("corpus_manifest_sha256"),
        "runner_inputs_sha256": result.get("runner_inputs_sha256"),
        "runner_scenario_bundle_sha256": result.get(
            "runner_scenario_bundle_sha256"
        ),
        "scenario_model_bundle_sha256": result.get(
            "scenario_model_bundle_sha256"
        ),
        "target_context_bundle_set_sha256": result.get(
            "target_context_bundle_set_sha256"
        ),
        "selection": result.get("selection"),
        "simulator_execution_started": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--bucket", choices=sorted(ALLOWED_BUCKETS), default="main_comparison")
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="verify and summarize runner inputs without launching a simulator",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = materialize_runner_inputs_v2(
        args.manifest,
        bucket=args.bucket,
        project_root=args.project_root,
    )
    # This module never executes a simulator.  The flag exists to make that
    # boundary explicit in reproducible commands; output is always a plan.
    print(json.dumps(_plan_document(result), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
