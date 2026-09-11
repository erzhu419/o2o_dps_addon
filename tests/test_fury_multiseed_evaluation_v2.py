from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import random
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

import o2o_dps.fury_multiseed_evaluation_v2 as evaluation_module
from o2o_dps.fury_multiseed_evaluation_v2 import (
    BASELINE_READINESS_FIELDS,
    FuryMultiseedProtocolError,
    ROLLOUT_REQUIRED_FIELDS,
    _bootstrap_lower_bound_summary,
    _cluster_bootstrap_lower_bound,
    _crossed_pigeonhole_bootstrap_summary,
    analyze_rollouts as _analyze_rollouts,
    build_baseline_comparison_readiness_receipt,
    build_plan,
    build_selection_evidence_bundle,
    corpus_binding_sha256,
    derive_seed_set,
    derive_simulator_seed,
    materialize_protocol as _materialize_protocol,
    selection_design_sha256,
    sha256_json,
)
from o2o_dps.fury_expert_adapters import CatFurySourceAdapter
from o2o_dps.fury_full_policy_rollout_v2 import run_fury_full_policy_rollout_v2
from o2o_dps.fury_paired_multiseed_runner_v2 import (
    COMPARISON_INTENT,
    DIAGNOSTIC_INTENT,
    FuryPairedRunnerError,
    ROLLOUT_KIND,
    SINGLE_BRIDGE_MODE,
    SYNTHETIC_MODE,
    build_exact_static_request_semantics_receipt,
    build_reduction_receipt,
    build_runner_plan,
    derive_simulator_seed as runner_derive_simulator_seed,
    execute_shard,
    reduce_shards,
    runner_scenario_bundle_sha256,
    runner_scenario_model_bundle_sha256,
    runner_target_context_bundle_set_sha256,
)
from o2o_dps.fury_selection_admission_v2 import (
    DISCOVERY_CAPTURE_MANIFEST_SCHEMA,
    DEVELOPMENT_EXCLUSION_SCHEMA,
    INSTANCE_EVIDENCE_MANIFEST_SCHEMA,
    FurySelectionAdmissionV2Error,
    build_candidate_seal,
    build_external_anchor_seal,
    build_final_corpus_admission_receipt,
    build_final_discovery_query,
    build_frozen_selection_shortlist,
    build_selection_receipt,
    canonical_json_bytes,
    replay_final_discovery_capture,
)

try:
    from tests.test_fury_full_policy_rollout_v2 import (
        _FullBridge,
        _CurrentBridge,
        _exact_context,
        _exact_context_receipt,
        _request,
    )
except ModuleNotFoundError:  # unittest discover -s tests imports top-level modules
    from test_fury_full_policy_rollout_v2 import (  # type: ignore[no-redef]
        _FullBridge,
        _CurrentBridge,
        _exact_context,
        _exact_context_receipt,
        _request,
    )


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_PATH = (
    PROJECT_ROOT / "configs" / "evaluation" / "fury_multiseed_protocol_v2.json"
)
CANDIDATE = "boc.fury.multiseed.fixture"


class _ProtocolFixture(dict[str, object]):
    trusted_external_anchor_seal_sha256s: tuple[str, ...] = ()
    selection_build_protocol: dict[str, object]
    selection_build_materialized: dict[str, object]
    selection_physical_replays: tuple[dict[str, object], ...]
    final_discovery_capture_manifest_path: Path


_PHYSICAL_RUN_DIRECTORIES: list[tempfile.TemporaryDirectory[str]] = []
_PHYSICAL_MANIFESTS_BY_ROW_SET: dict[
    tuple[str, str], tuple[Path, ...]
] = {}
_PHYSICAL_MANIFESTS_BY_PLAN: dict[str, tuple[Path, ...]] = {}
_PHYSICAL_ROWS_BY_VARIANT: dict[
    tuple[str, str, float, bool], list[dict[str, object]]
] = {}
_PROTOCOL_FIXTURE_CACHE: dict[
    tuple[bool, int, str, bool], _ProtocolFixture
] = {}


def _clone_protocol_fixture(value: _ProtocolFixture) -> _ProtocolFixture:
    cloned = _ProtocolFixture(copy.deepcopy(dict(value)))
    cloned.trusted_external_anchor_seal_sha256s = tuple(
        value.trusted_external_anchor_seal_sha256s
    )
    if hasattr(value, "selection_build_protocol"):
        cloned.selection_build_protocol = copy.deepcopy(
            value.selection_build_protocol
        )
        cloned.selection_build_materialized = copy.deepcopy(
            value.selection_build_materialized
        )
        cloned.selection_physical_replays = copy.deepcopy(
            value.selection_physical_replays
        )
    if hasattr(value, "final_discovery_capture_manifest_path"):
        cloned.final_discovery_capture_manifest_path = (
            value.final_discovery_capture_manifest_path
        )
    return cloned


def materialize_protocol(
    document: dict[str, object],
) -> dict[str, object]:
    return _materialize_protocol(
        document,
        trusted_external_anchor_seal_sha256s=getattr(
            document, "trusted_external_anchor_seal_sha256s", ()
        ),
        selection_physical_replays=getattr(
            document, "selection_physical_replays", ()
        ),
        final_discovery_capture_manifest_path=getattr(
            document, "final_discovery_capture_manifest_path", None
        ),
        _allow_nonpromoting_test_baseline_receipts=True,
    )


def _row_set_sha256(rows: list[dict[str, object]]) -> str:
    return sha256_json([str(row["row_sha256"]) for row in rows])


def _manifest_paths_for(
    runner_plan: dict[str, object], rows: list[dict[str, object]]
) -> tuple[Path, ...]:
    exact = _PHYSICAL_MANIFESTS_BY_ROW_SET.get(
        (str(runner_plan["plan_sha256"]), _row_set_sha256(rows))
    )
    if exact is not None:
        return exact
    return _PHYSICAL_MANIFESTS_BY_PLAN[str(runner_plan["plan_sha256"])]


def analyze_rollouts(
    materialized: dict[str, object],
    rows: list[dict[str, object]],
    *,
    runner_plan: dict[str, object],
    **kwargs: object,
) -> dict[str, object]:
    """Exercise the public analysis path through its mandatory reducer receipt."""

    try:
        manifest_paths = (
            None
            if runner_plan["contract"]["execution_mode"] == SYNTHETIC_MODE
            else _manifest_paths_for(runner_plan, rows)
        )
        receipt = build_reduction_receipt(
            runner_plan,
            rows,
            manifest_paths=manifest_paths,
        )
    except FuryPairedRunnerError as error:
        raise FuryMultiseedProtocolError(str(error)) from error
    return _analyze_rollouts(
        materialized,
        rows,
        runner_plan=runner_plan,
        reduction_receipt=receipt,
        manifest_paths=manifest_paths,
        **kwargs,
    )


def _protocol(
    *,
    eligible: bool = True,
    final_component_minimum: int = 1,
    protocol_state: str = "SEALED_FOR_EXECUTION",
    seal_selection: bool = True,
) -> _ProtocolFixture:
    cache_key = (
        eligible,
        final_component_minimum,
        protocol_state,
        seal_selection,
    )
    cached = _PROTOCOL_FIXTURE_CACHE.get(cache_key)
    if cached is not None:
        return _clone_protocol_fixture(cached)
    value = _ProtocolFixture(
        json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    )
    value["protocol_state"] = protocol_state
    phases = value["seed_contract"]["phases"]
    for offset, phase in enumerate(
        ("development", "selection_validation", "final_confirmation")
    ):
        phases[phase]["count"] = 200
        phases[phase]["counter_start"] = offset * 1000
        seeds = derive_seed_set(
            value["seed_contract"]["namespace"],
            phase,
            200,
            counter_start=offset * 1000,
        )
        phases[phase]["seed_list_sha256"] = sha256_json(list(seeds))

    statistics = value["evaluation_contract"]["statistics"]
    statistics["bootstrap_replicates"] = 5
    statistics["minimum_bootstrap_replicates"] = 5
    statistics["simultaneous_test_count"] = 9
    statistics["voting_bound_per_comparison"] = (
        "crossed_seed_x_guild_player_component_lower_bound"
    )
    statistics["marginal_cluster_bounds"] = "diagnostic_nonvoting"
    value["evaluation_contract"]["primary_minimum_horizon_ms"] = 2_000
    value["evaluation_contract"][
        "minimum_component_count_by_phase_and_stratum"
    ] = {
        phase: {
            "overall": 2,
            "single_target": (
                final_component_minimum
                if phase == "final_confirmation"
                else 1
            ),
            "multi_target": (
                final_component_minimum
                if phase == "final_confirmation"
                else 1
            ),
        }
        for phase in ("development", "selection_validation", "final_confirmation")
    }

    scenarios = _scenario_inputs()
    scenario_bundle_sha256 = runner_scenario_bundle_sha256(scenarios)
    scenario_model_bundle_sha256 = runner_scenario_model_bundle_sha256(
        scenarios
    )
    target_context_bundle_set_sha256 = (
        runner_target_context_bundle_set_sha256(scenarios)
    )
    phase_bindings = value["corpus_contract"]["phase_corpus_bindings"]
    for phase in ("development", "selection_validation", "final_confirmation"):
        binding = {
            "status": "FROZEN",
            "corpus_manifest_sha256": _digest("corpus", phase),
            "runner_inputs_sha256": _digest("runner-inputs", phase),
            "runner_scenario_bundle_sha256": scenario_bundle_sha256,
            "scenario_model_bundle_sha256": scenario_model_bundle_sha256,
            "target_context_bundle_set_sha256": (
                target_context_bundle_set_sha256
            ),
            "source_instance_provenance_sha256": _digest(
                "source-instance-provenance", phase
            ),
            "runner_scenario_count": len(scenarios),
            "comparison_eligible": True,
            "allowed_plan_intents": [COMPARISON_INTENT],
            "can_support_final_victory_claim": phase == "final_confirmation",
            "final_evidence": None,
            "blocker": None,
        }
        binding["corpus_binding_sha256"] = corpus_binding_sha256(binding)
        phase_bindings[phase] = binding
    final_contract = value["corpus_contract"]["final_confirmation_corpus"]
    final_contract.pop("minimum_new_instance_count", None)
    final_contract["required_take_first_n"] = 2
    final_contract["minimum_guild_player_leakage_component_count"] = 2

    source_identity = (
        evaluation_module.build_fury_execution_source_identity_v2()
    )
    source_contract = value["execution_contract"][
        "python_source_identity_contract"
    ]
    source_contract["required_entrypoints"] = sorted(
        evaluation_module.REQUIRED_PRODUCTION_PATHS
    )
    source_contract["file_count"] = source_identity["file_count"]
    source_contract["canonical_bundle_sha256"] = source_identity[
        "canonical_bundle"
    ]["sha256"]
    implementation = value["execution_contract"]["implementation_identity"]
    implementation["python_source_closure_sha256"] = source_identity[
        "canonical_bundle"
    ]["sha256"]
    for field, relative in (
        ("ordered_sink_executor_sha256", "o2o_dps/fury_ordered_sink_executor_v2.py"),
        ("full_policy_rollout_executor_sha256", "o2o_dps/fury_full_policy_rollout_v2.py"),
        ("paired_runner_source_sha256", "o2o_dps/fury_paired_multiseed_runner_v2.py"),
        ("evaluation_source_sha256", "o2o_dps/fury_multiseed_evaluation_v2.py"),
    ):
        implementation[field] = hashlib.sha256(
            (PROJECT_ROOT / relative).read_bytes()
        ).hexdigest()
    implementation["runtime_snapshot_sha256"] = value[
        "runtime_identity_contract"
    ]["snapshot_sha256"]

    for baseline in value["baseline_contract"]["required_baselines"]:
        baseline["policy_adapter_sha256"] = _digest(
            "adapter", baseline["policy_id"]
        )
        baseline["policy_profile_sha256"] = _digest(
            "profile", baseline["policy_id"]
        )
        baseline["comparison_eligible"] = eligible
        baseline["blocker"] = None if eligible else "fixture baseline blocked"
        baseline["comparison_eligibility_receipt"] = (
            build_baseline_comparison_readiness_receipt(
                baseline,
                runtime_snapshot_sha256=value["runtime_identity_contract"][
                    "snapshot_sha256"
                ],
                execution_bundle_identity=implementation,
                evidence_sha256_by_field={
                    field: _digest("readiness", baseline["policy_id"], field)
                    for field in BASELINE_READINESS_FIELDS
                },
            )
            if eligible
            else None
        )

    candidate_core = {
        "policy_id": CANDIDATE,
        "policy_source_sha256": _digest("candidate-source"),
        "policy_adapter_sha256": _digest("candidate-adapter"),
        "policy_profile_sha256": _digest("candidate-profile"),
    }
    registry_candidates = [dict(candidate_core)]
    value["development_candidate_registry"] = {
        "status": "FROZEN",
        "candidates": registry_candidates,
        "registry_sha256": sha256_json(registry_candidates),
        "blocker": None,
    }
    if not eligible or not seal_selection:
        value["candidate_contract"] = {
            "status": "NOT_FROZEN",
            "policy_id": None,
            "policy_source_sha256": None,
            "policy_adapter_sha256": None,
            "policy_profile_sha256": None,
            "selection_receipt_sha256": None,
            "candidate_seal_sha256": None,
            "external_anchor_seal_sha256": None,
            "frozen_at": None,
            "selected_without_final_confirmation_seeds": True,
            "blocker": "fixture selection is intentionally unavailable",
        }
        value["selection_contract"] = {
            "status": "NOT_FROZEN",
            "frozen_shortlist": None,
            "selection_evidence_bundle": None,
            "selection_receipt": None,
            "candidate_seal": None,
            "blocker": "fixture selection is intentionally unavailable",
        }
        phase_bindings["final_confirmation"][
            "can_support_final_victory_claim"
        ] = False
        _PROTOCOL_FIXTURE_CACHE[cache_key] = value
        return _clone_protocol_fixture(value)
    selection_binding = phase_bindings["selection_validation"]
    shortlist = build_frozen_selection_shortlist(
        candidates=[candidate_core],
        metric_id=evaluation_module.SELECTION_METRIC_ID,
        metric_definition_sha256=(
            evaluation_module.SELECTION_METRIC_DEFINITION_SHA256
        ),
        metric_direction="maximize",
        selection_once_nonce_sha256=_digest("selection-once"),
        seed_identity={
            "schema": "fury_seed_identity/v2",
            "phase": "selection_validation",
            "seed_count": phases["selection_validation"]["count"],
            "seed_list_sha256": phases["selection_validation"][
                "seed_list_sha256"
            ],
        },
        corpus_identity={
            "schema": "fury_corpus_identity/v2",
            "corpus_manifest_sha256": selection_binding[
                "corpus_manifest_sha256"
            ],
            "corpus_binding_sha256": selection_binding[
                "corpus_binding_sha256"
            ],
            "source_instance_provenance_sha256": selection_binding[
                "source_instance_provenance_sha256"
            ],
            "scenario_count": selection_binding["runner_scenario_count"],
        },
        runner_identity={
            "schema": "fury_runner_identity/v2",
            "runner_source_identity_sha256": source_identity[
                "canonical_bundle"
            ]["sha256"],
            "runner_inputs_sha256": selection_binding[
                "runner_inputs_sha256"
            ],
            "runner_scenario_bundle_sha256": selection_binding[
                "runner_scenario_bundle_sha256"
            ],
            "bridge_sha256": value["execution_contract"]["windows_local"][
                "bridge_sha256"
            ],
            "execution_bundle_sha256": sha256_json(implementation),
        },
    )
    value["selection_contract"] = {
        "status": "SHORTLIST_FROZEN",
        "frozen_shortlist": shortlist,
        "selection_evidence_bundle": None,
        "selection_receipt": None,
        "candidate_seal": None,
        "blocker": None,
    }
    candidate_identity = shortlist["candidates"][0]
    selection_build_protocol = _ProtocolFixture(copy.deepcopy(dict(value)))
    selection_stage_final_binding = selection_build_protocol[
        "corpus_contract"
    ]["phase_corpus_bindings"]["final_confirmation"]
    selection_stage_final_binding["can_support_final_victory_claim"] = False
    selection_stage_final_binding["corpus_binding_sha256"] = (
        corpus_binding_sha256(selection_stage_final_binding)
    )
    selection_materialized = materialize_protocol(selection_build_protocol)
    selection_plan = _runner_plan(
        selection_materialized, phase="selection_validation"
    )
    selection_rows = _rows(
        selection_materialized, phase="selection_validation"
    )
    selection_manifests = _manifest_paths_for(selection_plan, selection_rows)
    selection_reduction = reduce_shards(selection_plan, selection_manifests)
    selection_analysis = _analyze_rollouts(
        selection_materialized,
        selection_reduction["rollout_rows"],
        runner_plan=selection_plan,
        reduction_receipt=selection_reduction["reduction_receipt"],
        expected_protocol_sha256=selection_materialized["protocol_sha256"],
        phase="selection_validation",
        candidate_id=CANDIDATE,
        manifest_paths=selection_manifests,
        _allow_nonpromoting_test_selection_replay_authority=True,
    )
    selection_files = tempfile.TemporaryDirectory()
    _PHYSICAL_RUN_DIRECTORIES.append(selection_files)
    selection_root = Path(selection_files.name)
    selection_protocol_path = selection_root / "shortlist-protocol.json"
    selection_plan_path = selection_root / "runner-plan.json"
    selection_reduction_path = selection_root / "reduction.json"
    selection_analysis_path = selection_root / "analysis.json"
    selection_protocol_path.write_text(
        json.dumps(
            selection_build_protocol,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )
    selection_plan_path.write_text(
        json.dumps(selection_plan, ensure_ascii=False, indent=2, allow_nan=False)
        + "\n",
        encoding="utf-8",
    )
    selection_reduction_path.write_text(
        json.dumps(
            selection_reduction,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )
    selection_analysis_path.write_text(
        json.dumps(
            selection_analysis,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )
    selection_physical_replays = (
        {
            "candidate_id": CANDIDATE,
            "shortlist_protocol_path": selection_protocol_path,
            "runner_plan_path": selection_plan_path,
            "shard_manifest_paths": list(selection_manifests),
            "reduction_path": selection_reduction_path,
            "analysis_path": selection_analysis_path,
        },
    )
    evidence = build_selection_evidence_bundle(
        selection_build_protocol,
        [selection_analysis],
        materialized_protocol=selection_materialized,
        physical_replays=selection_physical_replays,
        _allow_nonpromoting_test_selection_replay_authority=True,
    )
    value.selection_build_protocol = selection_build_protocol
    value.selection_build_materialized = selection_materialized
    value.selection_physical_replays = selection_physical_replays
    receipt = build_selection_receipt(
        shortlist, metric_rows=evidence["metric_rows"]
    )
    external_anchor = build_external_anchor_seal(
        anchored_payload_sha256=receipt["selection_receipt_sha256"],
        anchored_at="2026-09-10T00:00:00Z",
        anchor_provider="fixture-trusted-anchor",
        anchor_reference="fixture://selection",
        anchor_evidence_sha256=_digest("anchor-evidence"),
    )
    candidate_seal = build_candidate_seal(receipt, external_anchor)
    value.trusted_external_anchor_seal_sha256s = (
        external_anchor["external_anchor_seal_sha256"],
    )
    value["selection_contract"] = {
        "status": "CANDIDATE_SEALED",
        "frozen_shortlist": shortlist,
        "selection_evidence_bundle": evidence,
        "selection_receipt": receipt,
        "candidate_seal": candidate_seal,
        "blocker": None,
    }
    sealed_identity = candidate_seal["candidate_identity"]
    value["candidate_contract"] = {
        "status": "FROZEN",
        "policy_id": sealed_identity["policy_id"],
        "policy_source_sha256": sealed_identity["policy_source_sha256"],
        "policy_adapter_sha256": sealed_identity["policy_adapter_sha256"],
        "policy_profile_sha256": sealed_identity["policy_profile_sha256"],
        "selected_without_final_confirmation_seeds": True,
        "selection_receipt_sha256": receipt["selection_receipt_sha256"],
        "candidate_seal_sha256": candidate_seal["candidate_seal_sha256"],
        "external_anchor_seal_sha256": external_anchor[
            "external_anchor_seal_sha256"
        ],
        "frozen_at": candidate_seal["frozen_at"],
        "blocker": None,
    }

    final_binding = phase_bindings["final_confirmation"]
    current_snapshot = value["corpus_contract"]["current_local_snapshot"]
    exclusion_core = {
        "schema": DEVELOPMENT_EXCLUSION_SCHEMA,
        "development_instance_count": current_snapshot[
            "completed_instance_count"
        ],
        "development_instance_id_sha256s": current_snapshot[
            "completed_instance_id_sha256s"
        ],
        "development_guild_player_component_sha256s": current_snapshot[
            "completed_guild_player_component_sha256s"
        ],
    }
    exclusion = {
        **exclusion_core,
        "development_exclusion_sha256": sha256_json(exclusion_core),
    }
    discovery_query = build_final_discovery_query(
        candidate_frozen_at=candidate_seal["frozen_at"],
        instance_name="Upper Tower of Karazhan",
        uploaded_before_inclusive="2026-09-13T00:00:00Z",
        page_size=50,
        take_first_n=2,
    )
    final_files = tempfile.TemporaryDirectory()
    _PHYSICAL_RUN_DIRECTORIES.append(final_files)
    final_root = Path(final_files.name)
    realm_id = "30000000-0000-4000-8000-000000000001"
    server_id = "30000000-0000-4000-8000-000000000002"
    instance_rows = [
        {
            "instance_id": "20000000-0000-4000-8000-000000000002",
            "uploaded_at": "2026-09-12T00:00:00Z",
            "started_at": "2026-09-02T00:00:00Z",
            "guild_id": "40000000-0000-4000-8000-000000000002",
            "player_guid": "Player-00000002",
        },
        {
            "instance_id": "10000000-0000-4000-8000-000000000001",
            "uploaded_at": "2026-09-11T00:00:00Z",
            "started_at": "2026-09-01T00:00:00Z",
            "guild_id": "40000000-0000-4000-8000-000000000001",
            "player_guid": "Player-00000001",
        },
    ]
    recent_activities = []
    evidence_paths = []
    for index, row in enumerate(instance_rows, start=1):
        activity = {
            "id": row["instance_id"],
            "slug": f"future-fixture-{index}",
            "name": "Upper Tower of Karazhan",
            "realm": {
                "id": realm_id,
                "server_id": server_id,
                "name": "Turtle WoW",
            },
            "guild": {"id": row["guild_id"], "name": f"Future Guild {index}"},
            "uploaded_at": row["uploaded_at"],
            "started_at": row["started_at"],
            "ended_at": "2026-09-02T01:00:00Z",
            "player_count": 1,
            "boss_count": 9,
            "boss_kills": 9,
            "has_youtube_video": False,
        }
        recent_activities.append(activity)
        metadata_path = final_root / f"instance-{index}-metadata.json"
        rankings_path = final_root / f"instance-{index}-rankings.json"
        activity_path = final_root / f"instance-{index}-all-activity.bin"
        evidence_path = final_root / f"instance-{index}-evidence.json"
        metadata_path.write_bytes(
            canonical_json_bytes(
                {
                    "id": row["instance_id"],
                    "realm_id": realm_id,
                    "name": "Upper Tower of Karazhan",
                    "guild": activity["guild"],
                    "players": {row["player_guid"]: {"name": f"Player {index}"}},
                    "encounters": [
                        {
                            "id": f"future-{index}-boss-{boss_index}",
                            "boss": True,
                            "kill_type": "clean",
                        }
                        for boss_index in range(9)
                    ],
                }
            )
        )
        rankings_path.write_bytes(
            canonical_json_bytes(
                [{"player_guid": row["player_guid"], "dps": 1000 + index}]
            )
        )
        activity_path.write_bytes(
            f"future-all-activity-{row['instance_id']}".encode("utf-8")
        )
        evidence_path.write_bytes(
            canonical_json_bytes(
                {
                    "schema": INSTANCE_EVIDENCE_MANIFEST_SCHEMA,
                    "instance_id": row["instance_id"],
                    "instance_metadata_path": metadata_path.name,
                    "ranking_records_path": rankings_path.name,
                    "all_activity_stream_path": activity_path.name,
                }
            )
        )
        evidence_paths.append(evidence_path)
    response_path = final_root / "recent-page-1.json"
    response_path.write_bytes(
        canonical_json_bytes(
            {
                "activities": recent_activities,
                "pagination": {"page": 1, "page_size": 50, "has_more": False},
            }
        )
    )
    request_query = urlencode(
        {
            "upload_after": discovery_query["uploaded_after_exclusive"],
            "instance_name": discovery_query["instance_name"],
            "page": 1,
            "page_size": 50,
        }
    )
    capture_manifest_path = final_root / "discovery-capture-manifest.json"
    capture_manifest_path.write_bytes(
        canonical_json_bytes(
            {
                "schema": DISCOVERY_CAPTURE_MANIFEST_SCHEMA,
                "query_sha256": sha256_json(discovery_query),
                "retrieved_at": "2026-09-13T01:00:00Z",
                "pages": [
                    {
                        "request_target": (
                            "/api/external/v1/raidlogs/recent?" + request_query
                        ),
                        "response_path": response_path.name,
                        "instance_evidence_manifest_paths": [
                            path.name for path in evidence_paths
                        ],
                    }
                ],
            }
        )
    )
    discovery_universe = replay_final_discovery_capture(
        discovery_query,
        candidate_frozen_at=str(candidate_seal["frozen_at"]),
        discovery_capture_manifest_path=capture_manifest_path,
    )
    future_records = [
        record
        for page in discovery_universe["pages"]
        for record in page["records"]
    ]
    provenance = [
        {
            "instance_id": row["instance_id"],
            "recent_activity_sha256": row["recent_activity_sha256"],
            "compact_instance_evidence_manifest_sha256": row[
                "compact_instance_evidence_manifest_sha256"
            ],
            "instance_metadata_sha256": row["instance_metadata_sha256"],
            "ranking_records_sha256": row["ranking_records_sha256"],
            "all_activity_stream_sha256": row["all_activity_stream_sha256"],
            "guild_player_component_sha256s": row[
                "guild_player_component_sha256s"
            ],
        }
        for row in future_records
    ]
    provenance.sort(key=lambda row: str(row["instance_id"]))
    final_binding["source_instance_provenance_sha256"] = sha256_json(provenance)
    final_binding["corpus_binding_sha256"] = corpus_binding_sha256(final_binding)
    value.final_discovery_capture_manifest_path = capture_manifest_path
    final_binding["final_evidence"] = build_final_corpus_admission_receipt(
        candidate_seal=candidate_seal,
        trusted_external_anchor_seal_sha256s=(
            value.trusted_external_anchor_seal_sha256s
        ),
        discovery_query=discovery_query,
        discovery_capture_manifest_path=capture_manifest_path,
        development_exclusion_snapshot=exclusion,
        selected_instance_ids=[
            row["instance_id"]
            for row in sorted(
                future_records,
                key=lambda item: (item["uploaded_at"], item["instance_id"]),
            )
        ],
        final_seed_identity={
            "schema": "fury_seed_identity/v2",
            "phase": "final_confirmation",
            "seed_count": phases["final_confirmation"]["count"],
            "seed_list_sha256": phases["final_confirmation"][
                "seed_list_sha256"
            ],
        },
        final_corpus_identity={
            "schema": "fury_corpus_identity/v2",
            "corpus_manifest_sha256": final_binding[
                "corpus_manifest_sha256"
            ],
            "corpus_binding_sha256": final_binding[
                "corpus_binding_sha256"
            ],
            "source_instance_provenance_sha256": final_binding[
                "source_instance_provenance_sha256"
            ],
            "scenario_count": final_binding["runner_scenario_count"],
        },
        final_runner_identity={
            "schema": "fury_runner_identity/v2",
            "runner_source_identity_sha256": source_identity[
                "canonical_bundle"
            ]["sha256"],
            "runner_inputs_sha256": final_binding["runner_inputs_sha256"],
            "runner_scenario_bundle_sha256": final_binding[
                "runner_scenario_bundle_sha256"
            ],
            "bridge_sha256": value["execution_contract"]["windows_local"][
                "bridge_sha256"
            ],
            "execution_bundle_sha256": sha256_json(implementation),
        },
    )
    _PROTOCOL_FIXTURE_CACHE[cache_key] = value
    return _clone_protocol_fixture(value)


def _digest(*values: object) -> str:
    return hashlib.sha256(repr(values).encode("utf-8")).hexdigest()


def _scenario_inputs() -> list[dict[str, object]]:
    scenarios = [
        {
            "instance_id": "10000000-0000-4000-8000-000000000001",
            "component_id": "component-a",
            "scenario_id": "boss-one",
            "stratum": "single_target",
            "scenario_weight": 1.0,
            "horizon_ms": 2_000,
            "estimated_cost_units": 2_000,
            "corpus_entry_sha256": _digest("corpus-entry", "boss-one"),
            "source_scenario_sha256": _digest("source-scenario", "boss-one"),
            "catalog_sha256": _digest("catalog", "future-a"),
            "request": _request(targets=1),
        },
        {
            "instance_id": "20000000-0000-4000-8000-000000000002",
            "component_id": "component-b",
            "scenario_id": "trash-many",
            "stratum": "multi_target",
            "scenario_weight": 1.0,
            "horizon_ms": 2_000,
            "estimated_cost_units": 2_000,
            "corpus_entry_sha256": _digest("corpus-entry", "trash-many"),
            "source_scenario_sha256": _digest("source-scenario", "trash-many"),
            "catalog_sha256": _digest("catalog", "future-b"),
            "request": _request(targets=2),
        },
    ]
    for scenario in scenarios:
        request = scenario["request"]
        request["encounter"]["duration"] = 2.0
        request["encounter"]["useHealth"] = False
        request_sha256 = sha256_json(request)
        target_count = len(request["encounter"]["targets"])
        contexts = [
            _exact_context_receipt(_exact_context(index))
            for index in range(target_count)
        ]
        target_bundle = {
            "schema_version": 2,
            "kind": "fury_target_context_bundle_v2",
            "binding_status": "EXACT_COMPARISON",
            "request_sha256": request_sha256,
            "target_count": target_count,
            "contexts": contexts,
            "comparison_eligible": True,
            "bridge_execution_eligible": True,
            "limitation_codes": [],
        }
        target_bundle_sha256 = sha256_json(target_bundle)
        scenario_model = {
            "schema_version": 2,
            "kind": "fury_runner_scenario_model_v2",
            "model_status": "COMPARISON_BOUND",
            "request_sha256": request_sha256,
            "target_context_bundle_sha256": target_bundle_sha256,
            "historical_truth": False,
            "comparison_eligible": True,
            "bridge_execution_eligible": True,
            "dynamic_armor_schedule_status": "EXACT_STATIC_SCENARIO",
            "dynamic_attackability_schedule_status": "EXACT_STATIC_SCENARIO",
            "health_or_horizon_status": "EXACT_FIXED_DURATION_SCENARIO",
            "dynamic_semantics_receipt": (
                build_exact_static_request_semantics_receipt(request)
            ),
            "limitation_codes": [],
        }
        scenario["target_context_bundle"] = target_bundle
        scenario["target_context_bundle_sha256"] = target_bundle_sha256
        scenario["scenario_model"] = scenario_model
        scenario["scenario_model_sha256"] = sha256_json(scenario_model)
    return scenarios


def _runner_plan(
    materialized: dict[str, object], *, phase: str
) -> dict[str, object]:
    protocol = materialized["protocol"]
    baseline_ids = materialized["required_baseline_ids"]
    baseline_entries = protocol["baseline_contract"]["required_baselines"]
    policies = [
        {
            "policy_id": entry["policy_id"],
            "source_sha256": entry["source_bundle_sha256"],
            "adapter_sha256": entry["policy_adapter_sha256"],
            "profile_sha256": entry["policy_profile_sha256"],
            "role": "BASELINE",
        }
        for entry in baseline_entries
    ]
    if phase == "development":
        candidate = next(
            row
            for row in protocol["development_candidate_registry"]["candidates"]
            if row["policy_id"] == CANDIDATE
        )
    elif phase == "selection_validation":
        shortlist = protocol["selection_contract"]["frozen_shortlist"]
        candidate = next(
            row for row in shortlist["candidates"] if row["policy_id"] == CANDIDATE
        )
    else:
        candidate = protocol["candidate_contract"]
    policies.append(
        {
            "policy_id": CANDIDATE,
            "source_sha256": candidate["policy_source_sha256"],
            "adapter_sha256": candidate["policy_adapter_sha256"],
            "profile_sha256": candidate["policy_profile_sha256"],
            "role": "CANDIDATE",
        }
    )
    binding = protocol["corpus_contract"]["phase_corpus_bindings"][phase]
    return build_runner_plan(
        protocol_id=protocol["protocol_id"],
        protocol_sha256=materialized["protocol_sha256"],
        phase=phase,
        corpus_manifest_sha256=binding["corpus_manifest_sha256"],
        runner_inputs_sha256=binding["runner_inputs_sha256"],
        runner_scenario_bundle_sha256=binding[
            "runner_scenario_bundle_sha256"
        ],
        corpus_binding_sha256=binding["corpus_binding_sha256"],
        master_seeds=materialized["seed_sets"][phase],
        scenarios=_scenario_inputs(),
        policies=policies,
        shard_count=1,
        bridge_identity={
            "sha256": protocol["execution_contract"]["windows_local"][
                "bridge_sha256"
            ],
            "platform": "windows-amd64",
        },
        execution_bundle_identity=protocol["execution_contract"][
            "implementation_identity"
        ],
        execution_mode=SINGLE_BRIDGE_MODE,
        seed_namespace=protocol["seed_contract"]["namespace"],
        plan_intent=COMPARISON_INTENT,
    )


class _ScaledFullBridge(_FullBridge):
    def __init__(self, scale: float) -> None:
        super().__init__()
        self._scale = scale

    def advance(self):
        before_damage = self.damage
        super().advance()
        self.damage = before_damage + (self.damage - before_damage) * self._scale
        return self._state()


def _replace_expert_id(value: object, expert_id: str) -> object:
    if isinstance(value, dict):
        return {
            key: _replace_expert_id(item, expert_id)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_replace_expert_id(item, expert_id) for item in value]
    if value == "cat.fury.profile1":
        return expert_id
    return value


def _rows(
    materialized: dict[str, object],
    *,
    phase: str,
    single_candidate_dps: float = 105.0,
    nonfaithful_first: bool = False,
) -> list[dict[str, object]]:
    plan = _runner_plan(materialized, phase=phase)
    variant = (
        str(plan["plan_sha256"]),
        phase,
        float(single_candidate_dps),
        nonfaithful_first,
    )
    cached = _PHYSICAL_ROWS_BY_VARIANT.get(variant)
    if cached is not None:
        return copy.deepcopy(cached)

    baseline_ids = materialized["required_baseline_ids"]
    baseline_dps = {
        baseline_ids[0]: 100.0,
        baseline_ids[1]: 95.0,
        baseline_ids[2]: 98.0,
    }
    expected_expert_ids = {
        "cat.fury.profile1": "cat.fury.profile1",
        "contra.deployed.fury.raid_a": "contra.deployed.fury.raid_a.v2",
        "contra260817.fury.source_candidate": (
            "contra260817.fury.source_candidate"
        ),
        CANDIDATE: CANDIDATE,
    }
    first_call = True

    def executor(*, group, scenario, policy):
        nonlocal first_call
        policy_id = str(policy["policy_id"])
        if policy_id == CANDIDATE:
            desired_dps = (
                single_candidate_dps
                if scenario["stratum"] == "single_target"
                else 110.0
            )
        else:
            desired_dps = baseline_dps[policy_id]
        bridge = (
            _CurrentBridge()
            if nonfaithful_first and first_call
            else _ScaledFullBridge(desired_dps / 100.0)
        )
        first_call = False
        contexts = {
            index: _exact_context(index)
            for index in range(
                len(scenario["request"]["encounter"]["targets"])
            )
        }
        artifact = run_fury_full_policy_rollout_v2(
            bridge,
            scenario["request"],
            CatFurySourceAdapter(),
            seed=group["simulator_seed"],
            target_contexts=contexts,
        )
        return {
            "full_policy_rollout": _replace_expert_id(
                artifact, expected_expert_ids[policy_id]
            )
        }

    temporary = tempfile.TemporaryDirectory()
    _PHYSICAL_RUN_DIRECTORIES.append(temporary)
    manifest = execute_shard(
        plan,
        0,
        executor,
        Path(temporary.name),
        execution_mode=SINGLE_BRIDGE_MODE,
        worker_id="evaluation-production-fixture",
    )
    reduction = reduce_shards(plan, (manifest,))
    rows = list(reduction["rollout_rows"])
    manifests = (manifest,)
    _PHYSICAL_MANIFESTS_BY_ROW_SET[
        (str(plan["plan_sha256"]), _row_set_sha256(rows))
    ] = manifests
    _PHYSICAL_MANIFESTS_BY_PLAN[str(plan["plan_sha256"])] = manifests
    _PHYSICAL_ROWS_BY_VARIANT[variant] = copy.deepcopy(rows)
    return copy.deepcopy(rows)


class FuryMultiseedEvaluationV2Tests(unittest.TestCase):
    @staticmethod
    def _replay_selection_evidence(
        document: _ProtocolFixture,
        analyses: list[dict[str, object]],
        *,
        physical_replays: list[dict[str, object]] | tuple[dict[str, object], ...],
    ) -> dict[str, object]:
        return build_selection_evidence_bundle(
            document.selection_build_protocol,
            analyses,
            materialized_protocol=document.selection_build_materialized,
            physical_replays=physical_replays,
            _allow_nonpromoting_test_selection_replay_authority=True,
        )

    @staticmethod
    def _bootstrap_pair(
        *,
        seed: int,
        component: str,
        candidate_dps: float,
        reference_dps: float = 100.0,
        weight: float = 1.0,
    ) -> tuple[SimpleNamespace, SimpleNamespace]:
        candidate = SimpleNamespace(
            master_seed=seed,
            component_id=component,
            scenario_weight=weight,
            dps=candidate_dps,
        )
        reference = SimpleNamespace(
            master_seed=seed,
            component_id=component,
            scenario_weight=weight,
            dps=reference_dps,
        )
        return candidate, reference

    @staticmethod
    def _scalar_crossed_bootstrap_estimates(
        pairs: list[tuple[SimpleNamespace, SimpleNamespace]],
        *,
        replicates: int,
        bootstrap_seed: int,
    ) -> list[float]:
        """Retained scalar oracle for the multiplicity-product definition."""

        cell_totals: dict[tuple[int, str], list[float]] = {}
        seed_keys: set[int] = set()
        component_keys: set[str] = set()
        for candidate, reference in pairs:
            key = (candidate.master_seed, candidate.component_id)
            seed_keys.add(candidate.master_seed)
            component_keys.add(candidate.component_id)
            totals = cell_totals.setdefault(key, [0.0, 0.0])
            totals[0] += candidate.scenario_weight * candidate.dps
            totals[1] += candidate.scenario_weight * reference.dps
        seeds = sorted(seed_keys)
        components = sorted(component_keys)
        cells = [
            (seed, component, totals[0], totals[1])
            for (seed, component), totals in sorted(cell_totals.items())
        ]
        rng = random.Random(bootstrap_seed)
        estimates: list[float] = []
        for _ in range(replicates):
            sampled_seeds = rng.choices(seeds, k=len(seeds))
            sampled_components = rng.choices(
                components,
                k=len(components),
            )
            candidate_total = 0.0
            reference_total = 0.0
            for seed, component, candidate_value, reference_value in cells:
                multiplicity = sampled_seeds.count(seed) * (
                    sampled_components.count(component)
                )
                if multiplicity:
                    candidate_total += multiplicity * candidate_value
                    reference_total += multiplicity * reference_value
            estimates.append(
                100.0
                * (candidate_total - reference_total)
                / reference_total
            )
        return estimates

    def test_crossed_bootstrap_uses_multiplicity_product_and_ratio_of_sums(
        self,
    ) -> None:
        pairs = [
            self._bootstrap_pair(
                seed=seed,
                component=component,
                candidate_dps=candidate_dps,
                reference_dps=reference_dps,
                weight=weight,
            )
            for seed, component, candidate_dps, reference_dps, weight in (
                (11, "a", 120.0, 100.0, 1.0),
                (11, "b", 90.0, 80.0, 2.0),
                (22, "a", 105.0, 100.0, 3.0),
                (22, "b", 140.0, 120.0, 1.0),
            )
        ]
        bootstrap_seed = 917
        rng = random.Random(bootstrap_seed)
        sampled_seeds = rng.choices([11, 22], k=2)
        sampled_components = rng.choices(["a", "b"], k=2)
        candidate_total = 0.0
        reference_total = 0.0
        for candidate, reference in pairs:
            multiplicity = sampled_seeds.count(candidate.master_seed) * (
                sampled_components.count(candidate.component_id)
            )
            candidate_total += (
                multiplicity * candidate.scenario_weight * candidate.dps
            )
            reference_total += (
                multiplicity * candidate.scenario_weight * reference.dps
            )
        expected = 100.0 * (
            candidate_total - reference_total
        ) / reference_total

        summary = _crossed_pigeonhole_bootstrap_summary(
            pairs,
            replicates=1,
            alpha=0.05,
            bootstrap_seed=bootstrap_seed,
        )
        self.assertAlmostEqual(summary["lower_bound_pct"], expected)
        self.assertEqual(
            summary["method"],
            "crossed_pigeonhole_seed_x_component_multiplicity_product",
        )
        self.assertEqual(
            summary["estimand"],
            "ratio_of_scenario_weighted_candidate_and_reference_sums",
        )
        self.assertEqual(summary["seed_cluster_count"], 2)
        self.assertEqual(summary["component_cluster_count"], 2)
        self.assertEqual(summary["observed_seed_component_cell_count"], 4)
        self.assertAlmostEqual(summary["effective_tail_draws"], 0.05)
        stability = summary["monte_carlo_stability_diagnostic"]
        self.assertTrue(stability["noninferential"])
        self.assertFalse(stability["used_by_gate"])
        self.assertIsNone(stability["second_partition_lower_bound_pct"])

    def test_crossed_bootstrap_detects_adversarial_two_way_random_effects(
        self,
    ) -> None:
        # The positive grand mean clears either one-axis bootstrap in
        # isolation, but it does not clear their joint seed x component
        # uncertainty.  This is the failure mode the old pair of marginal
        # bounds could miss.
        pairs = []
        for seed in range(20):
            seed_effect = -10.0 if seed < 10 else 10.0
            for component_index in range(20):
                component_effect = (
                    -10.0 if component_index < 10 else 10.0
                )
                pairs.append(
                    self._bootstrap_pair(
                        seed=seed,
                        component=f"component-{component_index:02d}",
                        candidate_dps=(
                            104.5 + seed_effect + component_effect
                        ),
                    )
                )

        alpha = 0.05
        replicates = 4000
        crossed = _crossed_pigeonhole_bootstrap_summary(
            pairs,
            replicates=replicates,
            alpha=alpha,
            bootstrap_seed=12345,
        )
        seed_marginal = _cluster_bootstrap_lower_bound(
            pairs,
            cluster="master_seed",
            replicates=replicates,
            alpha=alpha,
            bootstrap_seed=12346,
        )
        component_marginal = _cluster_bootstrap_lower_bound(
            pairs,
            cluster="component",
            replicates=replicates,
            alpha=alpha,
            bootstrap_seed=12347,
        )
        self.assertGreater(seed_marginal, 0.0)
        self.assertGreater(component_marginal, 0.0)
        self.assertLess(crossed["lower_bound_pct"], 0.0)
        stability = crossed["monte_carlo_stability_diagnostic"]
        self.assertTrue(stability["noninferential"])
        self.assertIsNotNone(
            stability["absolute_split_lower_bound_difference_pct"]
        )

    def test_crossed_bootstrap_acceleration_matches_scalar_oracle_and_is_repeatable(
        self,
    ) -> None:
        # Sparse cells plus two rows in one cell cover the aggregation and the
        # missing-cell zero semantics of the dense projection kernel.
        pairs = [
            self._bootstrap_pair(
                seed=seed,
                component=component,
                candidate_dps=candidate_dps,
                reference_dps=reference_dps,
                weight=weight,
            )
            for seed, component, candidate_dps, reference_dps, weight in (
                (11, "a", 109.0, 100.0, 1.0),
                (11, "a", 93.0, 97.0, 0.4),
                (11, "c", 112.0, 103.0, 0.7),
                (22, "b", 101.0, 99.0, 1.3),
                (22, "c", 98.0, 101.0, 0.9),
                (33, "a", 121.0, 108.0, 0.5),
                (33, "b", 96.0, 100.0, 1.8),
            )
        ]
        replicates = 61
        alpha = 0.17
        bootstrap_seed = 0x5EED
        expected = _bootstrap_lower_bound_summary(
            self._scalar_crossed_bootstrap_estimates(
                pairs,
                replicates=replicates,
                bootstrap_seed=bootstrap_seed,
            ),
            alpha=alpha,
        )
        actual = _crossed_pigeonhole_bootstrap_summary(
            pairs,
            replicates=replicates,
            alpha=alpha,
            bootstrap_seed=bootstrap_seed,
        )
        repeated = _crossed_pigeonhole_bootstrap_summary(
            pairs,
            replicates=replicates,
            alpha=alpha,
            bootstrap_seed=bootstrap_seed,
        )

        self.assertEqual(actual, repeated)
        self.assertAlmostEqual(
            actual["lower_bound_pct"],
            expected["lower_bound_pct"],
            places=12,
        )
        self.assertEqual(
            actual["effective_tail_draws"],
            expected["effective_tail_draws"],
        )
        actual_stability = actual["monte_carlo_stability_diagnostic"]
        expected_stability = expected["monte_carlo_stability_diagnostic"]
        for field in (
            "first_partition_lower_bound_pct",
            "second_partition_lower_bound_pct",
            "absolute_split_lower_bound_difference_pct",
        ):
            self.assertAlmostEqual(
                actual_stability[field],
                expected_stability[field],
                places=12,
            )
        for field in (
            "noninferential",
            "used_by_gate",
            "method",
            "first_partition_replicates",
            "second_partition_replicates",
        ):
            self.assertEqual(
                actual_stability[field],
                expected_stability[field],
            )

    def test_crossed_bootstrap_python_dispatch_scales_with_smaller_cluster_axis(
        self,
    ) -> None:
        # This is an operation-count guard, not a machine-dependent timer.  A
        # regression to the former per-replicate Python cell scan would visit
        # replicates * seeds * components cells.  The optimized path instead
        # performs at most two C-level dot-product dispatches per sampled
        # cluster on the smaller outer axis.
        seed_count = 128
        component_count = 7
        replicates = 40
        pairs = [
            self._bootstrap_pair(
                seed=seed,
                component=f"component-{component:02d}",
                candidate_dps=100.0 + ((seed * 17 + component * 31) % 23),
            )
            for seed in range(seed_count)
            for component in range(component_count)
        ]
        original_sumprod = evaluation_module._sumprod
        operand_lengths: list[tuple[int, int]] = []

        def tracked_sumprod(
            left: list[int],
            right: list[float],
        ) -> float:
            operand_lengths.append((len(left), len(right)))
            return original_sumprod(left, right)

        with patch.object(
            evaluation_module,
            "_sumprod",
            side_effect=tracked_sumprod,
        ):
            _crossed_pigeonhole_bootstrap_summary(
                pairs,
                replicates=replicates,
                alpha=0.05,
                bootstrap_seed=314159,
            )

        self.assertTrue(operand_lengths)
        self.assertTrue(
            all(
                lengths == (seed_count, seed_count)
                for lengths in operand_lengths
            )
        )
        self.assertLessEqual(
            len(operand_lengths),
            2 * replicates * component_count,
        )
        self.assertLess(
            len(operand_lengths),
            replicates * seed_count * component_count,
        )

    def test_evaluator_and_runner_share_one_request_seed_algorithm(self) -> None:
        namespace = "seed-contract-test"
        request_digest = _digest("request-seed-contract")
        self.assertEqual(
            derive_simulator_seed(namespace, 123456, request_digest),
            runner_derive_simulator_seed(
                123456,
                request_digest,
                namespace=namespace,
            ),
        )

    def test_committed_protocol_materializes_three_disjoint_seed_sets(self) -> None:
        document = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
        materialized = materialize_protocol(document)
        seeds = materialized["seed_sets"]
        self.assertEqual(len(seeds["development"]), 256)
        self.assertEqual(len(seeds["selection_validation"]), 256)
        self.assertEqual(len(seeds["final_confirmation"]), 1000)
        self.assertFalse(set(seeds["development"]) & set(seeds["selection_validation"]))
        self.assertFalse(set(seeds["development"]) & set(seeds["final_confirmation"]))
        self.assertFalse(
            set(seeds["selection_validation"]) & set(seeds["final_confirmation"])
        )
        self.assertEqual(
            set(materialized["protocol"]["evaluation_contract"]["rollout_row_required_fields"]),
            set(ROLLOUT_REQUIRED_FIELDS),
        )

    def test_analysis_requires_a_reducer_receipt(self) -> None:
        materialized = materialize_protocol(_protocol(eligible=True))
        plan = _runner_plan(materialized, phase="development")
        rows = _rows(materialized, phase="development")
        with self.assertRaisesRegex(TypeError, "reduction_receipt"):
            _analyze_rollouts(
                materialized,
                rows,
                runner_plan=plan,
                expected_protocol_sha256=materialized["protocol_sha256"],
                phase="development",
                candidate_id=CANDIDATE,
            )

    def test_analysis_rejects_a_receipt_for_a_different_row_set(self) -> None:
        materialized = materialize_protocol(_protocol(eligible=True))
        plan = _runner_plan(materialized, phase="development")
        rows = _rows(materialized, phase="development")
        manifests = _manifest_paths_for(plan, rows)
        receipt = build_reduction_receipt(
            plan, rows, manifest_paths=manifests
        )
        receipt["canonical_rollout_set_sha256"] = "0" * 64
        with self.assertRaisesRegex(
            FuryMultiseedProtocolError, "reduction receipt"
        ):
            _analyze_rollouts(
                materialized,
                rows,
                runner_plan=plan,
                reduction_receipt=receipt,
                manifest_paths=manifests,
                expected_protocol_sha256=materialized["protocol_sha256"],
                phase="development",
                candidate_id=CANDIDATE,
            )

    def test_synthetic_runner_rows_cannot_enter_the_statistical_gate(self) -> None:
        materialized = materialize_protocol(_protocol(eligible=True))
        protocol = materialized["protocol"]
        phase = "final_confirmation"
        baseline_entries = protocol["baseline_contract"]["required_baselines"]
        policies = [
            {
                "policy_id": entry["policy_id"],
                "source_sha256": entry["source_bundle_sha256"],
                "adapter_sha256": entry["policy_adapter_sha256"],
                "profile_sha256": entry["policy_profile_sha256"],
                "role": "BASELINE",
            }
            for entry in baseline_entries
        ]
        policies.append(
            {
                "policy_id": CANDIDATE,
                "source_sha256": protocol["candidate_contract"]["policy_source_sha256"],
                "adapter_sha256": protocol["candidate_contract"]["policy_adapter_sha256"],
                "profile_sha256": protocol["candidate_contract"]["policy_profile_sha256"],
                "role": "CANDIDATE",
            }
        )
        scenarios = _scenario_inputs()
        binding = protocol["corpus_contract"]["phase_corpus_bindings"][phase]
        plan = build_runner_plan(
            protocol_id=protocol["protocol_id"],
            protocol_sha256=materialized["protocol_sha256"],
            phase=phase,
            corpus_manifest_sha256=binding["corpus_manifest_sha256"],
            runner_inputs_sha256=binding["runner_inputs_sha256"],
            runner_scenario_bundle_sha256=binding[
                "runner_scenario_bundle_sha256"
            ],
            corpus_binding_sha256=binding["corpus_binding_sha256"],
            master_seeds=materialized["seed_sets"][phase],
            scenarios=scenarios,
            policies=policies,
            shard_count=2,
            bridge_identity={
                "sha256": protocol["execution_contract"]["windows_local"]["bridge_sha256"],
                "platform": "windows-amd64",
            },
            execution_bundle_identity=protocol["execution_contract"][
                "implementation_identity"
            ],
            execution_mode=SYNTHETIC_MODE,
            seed_namespace=protocol["seed_contract"]["namespace"],
        )

        baseline_dps = {
            baseline_entries[0]["policy_id"]: 100.0,
            baseline_entries[1]["policy_id"]: 95.0,
            baseline_entries[2]["policy_id"]: 98.0,
        }

        def executor(*, group, scenario, policy):
            dps = 110.0 if policy["role"] == "CANDIDATE" else baseline_dps[policy["policy_id"]]
            elapsed = int(scenario["horizon_ms"])
            return {
                "damage": dps * elapsed / 1000.0,
                "elapsed_ms": elapsed,
                "dps": dps,
                "completion_criterion_met": True,
                "evaluation_eligible": True,
                "omitted_lane_count": 0,
                "fatal_error_count": 0,
                "nonfaithful_reason_counts": {},
                "end_state_sha256": _digest(
                    "runner-end", group["group_id"], policy["policy_id"]
                ),
            }

        with tempfile.TemporaryDirectory() as temporary:
            manifests = [
                execute_shard(
                    plan,
                    shard,
                    executor,
                    Path(temporary) / f"shard-{shard}",
                    execution_mode=SYNTHETIC_MODE,
                )
                for shard in range(2)
            ]
            reduced = reduce_shards(plan, manifests)
        self.assertTrue(reduced["complete_cartesian_product"])
        with self.assertRaisesRegex(
            FuryMultiseedProtocolError, "execution_mode"
        ):
            analyze_rollouts(
                materialized,
                reduced["rollout_rows"],
                runner_plan=plan,
                expected_protocol_sha256=materialized["protocol_sha256"],
                phase=phase,
                candidate_id=CANDIDATE,
            )

    def test_seed_digest_tamper_is_rejected(self) -> None:
        document = _protocol()
        document["seed_contract"]["phases"]["development"][
            "seed_list_sha256"
        ] = "0" * 64
        with self.assertRaisesRegex(FuryMultiseedProtocolError, "mismatch"):
            materialize_protocol(document)

    def test_final_admission_receipt_tamper_is_rejected(self) -> None:
        document = _protocol()
        document["corpus_contract"]["phase_corpus_bindings"][
            "final_confirmation"
        ]["final_evidence"]["final_admission_receipt_sha256"] = "0" * 64
        with self.assertRaisesRegex(
            FuryMultiseedProtocolError, "not content-addressed"
        ):
            materialize_protocol(document)

    def test_final_admission_requires_out_of_band_physical_capture(self) -> None:
        document = _protocol()
        del document.final_discovery_capture_manifest_path
        with self.assertRaisesRegex(
            FuryMultiseedProtocolError, "discovery_capture_manifest_path"
        ):
            materialize_protocol(document)

    def test_selection_evidence_requires_every_preregistered_cell(self) -> None:
        document = _protocol()
        analyses = copy.deepcopy(
            document["selection_contract"]["selection_evidence_bundle"][
                "analysis_artifacts"
            ]
        )
        analysis = analyses[0]
        analysis.pop("analysis_sha256")
        analysis["comparisons"].pop()
        analysis["analysis_sha256"] = sha256_json(analysis)
        with self.assertRaisesRegex(
            FuryMultiseedProtocolError, "physical analysis differs"
        ):
            self._replay_selection_evidence(
                document,
                analyses,
                physical_replays=document.selection_physical_replays,
            )

    def test_selection_evidence_rejects_corpus_identity_substitution(self) -> None:
        document = _protocol()
        analyses = copy.deepcopy(
            document["selection_contract"]["selection_evidence_bundle"][
                "analysis_artifacts"
            ]
        )
        analysis = analyses[0]
        analysis.pop("analysis_sha256")
        analysis["corpus_identity"][
            "source_instance_provenance_sha256"
        ] = _digest("substituted-source-provenance")
        analysis["analysis_sha256"] = sha256_json(analysis)
        with self.assertRaisesRegex(
            FuryMultiseedProtocolError, "physical analysis differs"
        ):
            self._replay_selection_evidence(
                document,
                analyses,
                physical_replays=document.selection_physical_replays,
            )

    def test_selection_evidence_requires_physical_replay_for_every_candidate(self) -> None:
        document = _protocol()
        analyses = copy.deepcopy(
            document["selection_contract"]["selection_evidence_bundle"][
                "analysis_artifacts"
            ]
        )
        with self.assertRaisesRegex(
            FuryMultiseedProtocolError, "exactly one physical replay per candidate"
        ):
            self._replay_selection_evidence(
                document,
                analyses,
                physical_replays=[],
            )

    def test_selection_evidence_rejects_missing_physical_manifest(self) -> None:
        document = _protocol()
        analyses = copy.deepcopy(
            document["selection_contract"]["selection_evidence_bundle"][
                "analysis_artifacts"
            ]
        )
        replay = copy.deepcopy(document.selection_physical_replays[0])
        replay["shard_manifest_paths"] = [
            Path(str(replay["runner_plan_path"])).parent / "missing-manifest.json"
        ]
        with self.assertRaisesRegex(
            FuryMultiseedProtocolError, r"shard manifest\[0\].*does not exist"
        ):
            self._replay_selection_evidence(
                document,
                analyses,
                physical_replays=[replay],
            )

    def test_selection_evidence_rejects_tampered_physical_rollout_rows(self) -> None:
        document = _protocol()
        analyses = copy.deepcopy(
            document["selection_contract"]["selection_evidence_bundle"][
                "analysis_artifacts"
            ]
        )
        replay = copy.deepcopy(document.selection_physical_replays[0])
        source_manifest = Path(replay["shard_manifest_paths"][0])
        with tempfile.TemporaryDirectory() as temporary:
            copied_root = Path(temporary) / "shard"
            shutil.copytree(source_manifest.parent, copied_root)
            copied_manifest = copied_root / source_manifest.name
            manifest = json.loads(copied_manifest.read_text(encoding="utf-8"))
            rollout_path = copied_root / manifest["rollout_jsonl"]["file"]
            rollout_path.write_bytes(rollout_path.read_bytes() + b" ")
            replay["shard_manifest_paths"] = [copied_manifest]
            with self.assertRaisesRegex(
                FuryMultiseedProtocolError,
                "physical replay failed runner validation",
            ):
                self._replay_selection_evidence(
                    document,
                    analyses,
                    physical_replays=[replay],
                )

    def test_selection_evidence_replays_positive_physical_fixture(self) -> None:
        document = _protocol()
        expected = document["selection_contract"]["selection_evidence_bundle"]
        rebuilt = self._replay_selection_evidence(
            document,
            copy.deepcopy(expected["analysis_artifacts"]),
            physical_replays=document.selection_physical_replays,
        )
        self.assertEqual(rebuilt, expected)
        self.assertEqual(len(rebuilt["physical_replay_receipts"]), 1)

    def test_sealed_selection_rejects_self_consistent_receipts_without_replay_package(
        self,
    ) -> None:
        document = _protocol()
        with self.assertRaisesRegex(
            FuryMultiseedProtocolError,
            "sealed selection requires out-of-band physical replay packages",
        ):
            _materialize_protocol(
                document,
                trusted_external_anchor_seal_sha256s=(
                    document.trusted_external_anchor_seal_sha256s
                ),
                _allow_nonpromoting_test_baseline_receipts=True,
            )

    def test_sealed_selection_rejects_fabricated_content_addressed_replay_receipt(
        self,
    ) -> None:
        document = _protocol()
        attacked = _ProtocolFixture(copy.deepcopy(dict(document)))
        attacked.trusted_external_anchor_seal_sha256s = (
            document.trusted_external_anchor_seal_sha256s
        )
        attacked.selection_physical_replays = document.selection_physical_replays
        bundle = attacked["selection_contract"]["selection_evidence_bundle"]
        replay_receipt = bundle["physical_replay_receipts"][0]
        replay_receipt.pop("physical_replay_receipt_sha256")
        replay_receipt["runner_plan_file_sha256"] = "0" * 64
        replay_receipt["physical_replay_receipt_sha256"] = sha256_json(
            replay_receipt
        )
        bundle.pop("selection_evidence_bundle_sha256")
        bundle["selection_evidence_bundle_sha256"] = sha256_json(bundle)
        with self.assertRaisesRegex(
            FuryMultiseedProtocolError,
            "selection evidence bundle is inconsistent",
        ):
            materialize_protocol(attacked)

    def test_sealed_final_contract_cannot_change_registered_first_n(self) -> None:
        document = _protocol()
        document["corpus_contract"]["final_confirmation_corpus"][
            "required_take_first_n"
        ] = 3
        with self.assertRaisesRegex(
            FuryMultiseedProtocolError,
            "changed preregistered selection-stage invariants",
        ):
            materialize_protocol(document)

    def test_final_admission_rejects_a_development_instance(self) -> None:
        document = _protocol()
        evidence = document["corpus_contract"]["phase_corpus_bindings"][
            "final_confirmation"
        ]["final_evidence"]
        exclusion = copy.deepcopy(evidence["development_exclusion_snapshot"])
        reused_id = evidence["selection_replay"]["selected_instances"][0][
            "instance_id"
        ]
        reused_hash = hashlib.sha256(reused_id.encode("utf-8")).hexdigest()
        exclusion["development_instance_id_sha256s"] = sorted(
            [*exclusion["development_instance_id_sha256s"], reused_hash]
        )
        exclusion["development_instance_count"] = len(
            exclusion["development_instance_id_sha256s"]
        )
        exclusion_core = {
            key: value
            for key, value in exclusion.items()
            if key != "development_exclusion_sha256"
        }
        exclusion["development_exclusion_sha256"] = sha256_json(
            exclusion_core
        )
        identities = evidence["final_input_identities"]
        with self.assertRaisesRegex(
            FurySelectionAdmissionV2Error,
            "eligible instances|first-N eligible",
        ):
            build_final_corpus_admission_receipt(
                candidate_seal=evidence["candidate_seal"],
                trusted_external_anchor_seal_sha256s=(
                    document.trusted_external_anchor_seal_sha256s
                ),
                discovery_query=evidence["discovery_query"],
                discovery_capture_manifest_path=(
                    document.final_discovery_capture_manifest_path
                ),
                development_exclusion_snapshot=exclusion,
                selected_instance_ids=[
                    row["instance_id"]
                    for row in evidence["selection_replay"][
                        "selected_instances"
                    ]
                ],
                final_seed_identity=identities["seed"],
                final_corpus_identity=identities["corpus"],
                final_runner_identity=identities["runner"],
            )

    def test_baseline_boolean_cannot_promote_without_a_readiness_receipt(self) -> None:
        document = _protocol(eligible=False)
        baseline = document["baseline_contract"]["required_baselines"][2]
        baseline["comparison_eligible"] = True
        baseline["policy_adapter_sha256"] = _digest("invented-adapter")
        baseline["policy_profile_sha256"] = _digest("invented-profile")
        with self.assertRaisesRegex(
            FuryMultiseedProtocolError, "comparison_eligibility_receipt"
        ):
            materialize_protocol(document)

    def test_self_reported_readiness_hashes_cannot_promote_in_production(self) -> None:
        document = _protocol(eligible=False)
        baseline = document["baseline_contract"]["required_baselines"][0]
        baseline["comparison_eligible"] = True
        baseline["blocker"] = None
        receipt = build_baseline_comparison_readiness_receipt(
            baseline,
            runtime_snapshot_sha256=document["runtime_identity_contract"][
                "snapshot_sha256"
            ],
            execution_bundle_identity=document["execution_contract"][
                "implementation_identity"
            ],
            evidence_sha256_by_field={
                field: _digest("unverified-readiness", field)
                for field in BASELINE_READINESS_FIELDS
            },
        )
        baseline["comparison_eligibility_receipt"] = receipt
        self.assertEqual(receipt["evidence_class"], "SELF_REPORTED_DIGEST_INDEX")
        self.assertEqual(receipt["promotion_status"], "NONPROMOTING")
        self.assertFalse(receipt["comparison_eligible"])
        with self.assertRaisesRegex(
            FuryMultiseedProtocolError,
            "self-reported baseline readiness digest receipt is explicitly non-promoting",
        ):
            _materialize_protocol(
                document,
            )
        diagnostic = _materialize_protocol(
            document,
            _allow_nonpromoting_test_baseline_receipts=True,
        )
        self.assertEqual(
            diagnostic["baseline_comparison_authority"]["status"],
            "NONPROMOTING_TEST_FIXTURE",
        )
        self.assertFalse(
            diagnostic["baseline_comparison_authority"][
                "typed_physical_production_evidence_verified"
            ]
        )
        plan = build_plan(diagnostic)
        self.assertIn(
            "typed_physical_baseline_production_evidence_not_verified",
            plan["blocked_reasons"],
        )
        self.assertTrue(
            all(
                phase["dispatch_allowed"] is False
                for phase in plan["readiness"]["phases"].values()
            )
        )

    def test_baseline_authority_envelope_cannot_self_promote(self) -> None:
        materialized = materialize_protocol(_protocol(eligible=False))
        materialized["baseline_comparison_authority"] = {
            "status": "PHYSICAL_EVIDENCE_VERIFIED",
            "typed_physical_production_evidence_verified": True,
            "victory_claim_capable": True,
        }
        with self.assertRaisesRegex(
            FuryMultiseedProtocolError, "unknown baseline comparison authority"
        ):
            build_plan(materialized)

    def test_private_selection_replay_authority_cannot_enter_final_phase(self) -> None:
        materialized = materialize_protocol(_protocol(eligible=False))
        with self.assertRaisesRegex(
            FuryMultiseedProtocolError,
            "restricted to selection-validation unit-test envelopes",
        ):
            _analyze_rollouts(
                materialized,
                [],
                runner_plan={},
                reduction_receipt={},
                expected_protocol_sha256=materialized["protocol_sha256"],
                phase="final_confirmation",
                candidate_id=CANDIDATE,
                _allow_nonpromoting_test_selection_replay_authority=True,
            )

    def test_materialized_seed_envelope_cannot_be_trimmed_after_validation(self) -> None:
        materialized = materialize_protocol(_protocol(eligible=True))
        plan = _runner_plan(materialized, phase="final_confirmation")
        rows = _rows(materialized, phase="final_confirmation")
        tampered = copy.deepcopy(materialized)
        tampered["seed_sets"]["final_confirmation"] = tampered["seed_sets"][
            "final_confirmation"
        ][:1]
        with self.assertRaisesRegex(
            FuryMultiseedProtocolError, "modified after validation"
        ):
            analyze_rollouts(
                tampered,
                rows,
                runner_plan=plan,
                expected_protocol_sha256=materialized["protocol_sha256"],
                phase="final_confirmation",
                candidate_id=CANDIDATE,
            )

    def test_self_declared_seal_cannot_replace_the_external_protocol_pin(self) -> None:
        materialized = materialize_protocol(_protocol(eligible=True))
        with self.assertRaisesRegex(
            FuryMultiseedProtocolError, "externally pinned execution seal"
        ):
            _analyze_rollouts(
                materialized,
                [],
                runner_plan={},
                reduction_receipt={},
                expected_protocol_sha256=_digest("older-preregistered-protocol"),
                phase="final_confirmation",
                candidate_id=CANDIDATE,
            )

    def test_runtime_identity_hash_and_authority_promotion_are_rejected(self) -> None:
        bad_hash = _protocol()
        bad_hash["runtime_identity_contract"]["cat_profile1_semantic_sha256"] = (
            "x" * 64
        )
        with self.assertRaisesRegex(
            FuryMultiseedProtocolError, "cat_profile1_semantic_sha256"
        ):
            materialize_protocol(bad_hash)

        promoted = _protocol()
        promoted["runtime_identity_contract"][
            "comparison_eligible_by_itself"
        ] = True
        with self.assertRaisesRegex(FuryMultiseedProtocolError, "cannot promote"):
            materialize_protocol(promoted)

    def test_live_python_execution_closure_is_not_caller_declared(self) -> None:
        document = _protocol()
        document["execution_contract"]["python_source_identity_contract"][
            "canonical_bundle_sha256"
        ] = "0" * 64
        document["execution_contract"]["implementation_identity"][
            "python_source_closure_sha256"
        ] = "0" * 64
        with self.assertRaisesRegex(
            FuryMultiseedProtocolError, "live Python execution closure"
        ):
            materialize_protocol(document)

    def test_plan_is_explicitly_nonexecuting_and_nonclaiming(self) -> None:
        materialized = materialize_protocol(_protocol(eligible=False))
        plan = build_plan(materialized)
        self.assertFalse(plan["execution_started"])
        self.assertFalse(plan["victory_claim_allowed"])
        for policy_id in materialized["required_baseline_ids"]:
            self.assertIn(
                f"required_baseline_not_comparison_eligible:{policy_id}",
                plan["blocked_reasons"],
            )
        self.assertEqual(plan["seed_contract"]["final_confirmation"]["count"], 200)
        self.assertFalse(
            plan["readiness"]["phases"]["development"]["dispatch_allowed"]
        )
        self.assertEqual(
            3,
            sum(
                not row["comparison_eligible"]
                for row in plan["readiness"]["required_baselines"]
            ),
        )

    def test_committed_plan_reports_frozen_development_corpus_only(self) -> None:
        materialized = materialize_protocol(
            json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
        )
        plan = build_plan(materialized)
        phases = plan["readiness"]["phases"]
        self.assertEqual("FROZEN", phases["development"]["corpus_status"])
        self.assertEqual("NOT_FROZEN", phases["selection_validation"]["corpus_status"])
        self.assertEqual("NOT_AVAILABLE", phases["final_confirmation"]["corpus_status"])
        self.assertTrue(all(not row["dispatch_allowed"] for row in phases.values()))
        self.assertEqual("NOT_DISPATCHED", plan["readiness"]["hpc_status"])
        capsule = plan["corpus_contract"]["phase_corpus_bindings"][
            "development"
        ]["scenario_model_capsule"]
        self.assertEqual(1197, capsule["scenario_count"])
        self.assertEqual(3269, capsule["target_count"])
        self.assertEqual(0, capsule["raw_csv_or_normalized_bytes_uploaded"])
        self.assertEqual(
            "BLOCKED_STATIC_CONTROL_BINDING_ONLY",
            capsule["dynamic_target_schedule_simulator_binding"],
        )
        binding = capsule["static_control_binding"]
        self.assertEqual(2474060, binding["size_bytes"])
        self.assertEqual(1197, binding["scenario_count"])
        self.assertEqual(3269, binding["target_count"])
        self.assertEqual(8435, binding["dynamic_armor_transition_count_excluded"])
        self.assertEqual(
            9807, binding["non_full_wave_attackability_branch_count_excluded"]
        )
        self.assertFalse(binding["historical_truth"])
        self.assertFalse(binding["comparison_eligible"])
        self.assertFalse(binding["simulator_execution_started"])
        target_model = plan["corpus_contract"]["compact_target_model_contract"]
        self.assertFalse(target_model["historical_truth"])
        self.assertFalse(target_model["raw_or_normalized_upload_required"])
        self.assertEqual(
            "MISSING_REQUIRES_PLAYER_CONDITIONING",
            target_model["background_team_damage"],
        )
        self.assertIn("protocol_not_sealed_for_execution", plan["blocked_reasons"])
        self.assertIn(
            "candidate_policy_identity_not_frozen", plan["blocked_reasons"]
        )
        self.assertIn(
            "phase_corpus_not_frozen:selection_validation",
            plan["blocked_reasons"],
        )
        self.assertIn(
            "final_corpus_not_untouched_claim_eligible",
            plan["blocked_reasons"],
        )

    def test_final_gate_requires_every_baseline_and_both_strata(self) -> None:
        materialized = materialize_protocol(_protocol(eligible=True))
        artifact = analyze_rollouts(
            materialized,
            _rows(materialized, phase="final_confirmation"),
            runner_plan=_runner_plan(materialized, phase="final_confirmation"),
            expected_protocol_sha256=materialized["protocol_sha256"],
            phase="final_confirmation",
            candidate_id=CANDIDATE,
        )
        self.assertFalse(artifact["simulator_multiseed_gate_passed"])
        self.assertFalse(artifact["victory_claim_allowed"])
        self.assertIn(
            "typed physical baseline production evidence is not verified",
            artifact["blocked_reasons"],
        )
        self.assertEqual(len(artifact["comparisons"]), 9)
        self.assertEqual(
            artifact["statistical_contract"]["simultaneous_test_count"], 9
        )
        self.assertAlmostEqual(
            artifact["statistical_contract"]["per_test_alpha"], 0.05 / 9
        )
        self.assertAlmostEqual(
            artifact["statistical_contract"]["effective_tail_draws"],
            5 * 0.05 / 9,
        )
        self.assertEqual(
            artifact["statistical_contract"]["marginal_cluster_bounds"],
            "diagnostic_nonvoting",
        )
        for row in artifact["comparisons"]:
            self.assertEqual(
                row["crossed_seed_component_lower_bound_pct"],
                row["simultaneous_one_sided_lower_bound_pct"],
            )
            self.assertTrue(row["diagnostic_nonvoting"]["noninferential"])
            self.assertFalse(row["diagnostic_nonvoting"]["used_by_gate"])
            self.assertTrue(row["descriptive_pair_row_noninferential"])
            self.assertNotIn("wins", row)
            self.assertNotIn("ties", row)
            self.assertNotIn("losses", row)
            self.assertNotIn("mean_paired_dps_delta", row)
        self.assertTrue(
            all(
                row["paired_relative_dps_improvement_pct"]
                >= row["minimum_practical_mean_improvement_pct"]
                and row["crossed_seed_component_lower_bound_pct"]
                > row["required_lower_bound_pct"]
                for row in artifact["comparisons"]
            )
        )
        self.assertTrue(
            all(not row["comparison_passed"] for row in artifact["comparisons"])
        )

    def test_marginal_cluster_diagnostics_cannot_vote(self) -> None:
        materialized = materialize_protocol(_protocol(eligible=True))

        def final_only_diagnostic_override(pairs, **kwargs):
            if pairs and pairs[0][0].phase == "final_confirmation":
                return -999.0
            return _cluster_bootstrap_lower_bound(pairs, **kwargs)

        with patch(
            "o2o_dps.fury_multiseed_evaluation_v2._cluster_bootstrap_lower_bound",
            side_effect=final_only_diagnostic_override,
        ):
            artifact = analyze_rollouts(
                materialized,
                _rows(materialized, phase="final_confirmation"),
                runner_plan=_runner_plan(
                    materialized, phase="final_confirmation"
                ),
                expected_protocol_sha256=materialized["protocol_sha256"],
                phase="final_confirmation",
                candidate_id=CANDIDATE,
            )
        self.assertFalse(artifact["simulator_multiseed_gate_passed"])
        self.assertFalse(artifact["victory_claim_allowed"])
        self.assertIn(
            "typed physical baseline production evidence is not verified",
            artifact["blocked_reasons"],
        )
        for row in artifact["comparisons"]:
            diagnostics = row["diagnostic_nonvoting"]
            self.assertEqual(
                diagnostics["simulator_seed_marginal_lower_bound_pct"],
                -999.0,
            )
            self.assertEqual(
                diagnostics["corpus_component_marginal_lower_bound_pct"],
                -999.0,
            )
            self.assertGreater(
                row["crossed_seed_component_lower_bound_pct"],
                row["required_lower_bound_pct"],
            )
            self.assertFalse(row["comparison_passed"])

    def test_single_target_regression_blocks_pooled_victory(self) -> None:
        materialized = materialize_protocol(_protocol(eligible=True))
        artifact = analyze_rollouts(
            materialized,
            _rows(
                materialized,
                phase="final_confirmation",
                single_candidate_dps=99.0,
            ),
            runner_plan=_runner_plan(materialized, phase="final_confirmation"),
            expected_protocol_sha256=materialized["protocol_sha256"],
            phase="final_confirmation",
            candidate_id=CANDIDATE,
        )
        self.assertFalse(artifact["simulator_multiseed_gate_passed"])
        self.assertFalse(artifact["victory_claim_allowed"])
        cat_single = next(
            row
            for row in artifact["comparisons"]
            if row["baseline_id"] == "cat.fury.profile1"
            and row["stratum"] == "single_target"
        )
        self.assertFalse(cat_single["comparison_passed"])
        self.assertLess(cat_single["paired_relative_dps_improvement_pct"], 0)

    def test_each_stratum_requires_its_own_component_support(self) -> None:
        document = _protocol(eligible=True, final_component_minimum=2)
        materialized = materialize_protocol(document)
        artifact = analyze_rollouts(
            materialized,
            _rows(materialized, phase="final_confirmation"),
            runner_plan=_runner_plan(
                materialized, phase="final_confirmation"
            ),
            expected_protocol_sha256=materialized["protocol_sha256"],
            phase="final_confirmation",
            candidate_id=CANDIDATE,
        )
        self.assertFalse(artifact["simulator_multiseed_gate_passed"])
        self.assertFalse(artifact["victory_claim_allowed"])
        self.assertTrue(
            any(
                "/single_target has 1 corpus components" in reason
                for reason in artifact["blocked_reasons"]
            )
        )
        self.assertTrue(
            any(
                "/multi_target has 1 corpus components" in reason
                for reason in artifact["blocked_reasons"]
            )
        )

    def test_unsealed_required_baseline_blocks_claim_even_with_high_dps(self) -> None:
        materialized = materialize_protocol(_protocol(eligible=False))
        plan = build_plan(materialized)
        self.assertFalse(
            plan["readiness"]["phases"]["final_confirmation"][
                "dispatch_allowed"
            ]
        )
        self.assertEqual(
            plan["readiness"]["candidate_status"], "NOT_FROZEN"
        )
        self.assertEqual(
            3,
            sum(
                not row["comparison_eligible"]
                for row in plan["readiness"]["required_baselines"]
            ),
        )

    def test_draft_protocol_blocks_claim_even_when_rows_and_policies_pass(self) -> None:
        document = _protocol(
            eligible=True,
            protocol_state="DRAFT_FOR_REVIEW",
            seal_selection=False,
        )
        materialized = materialize_protocol(document)
        artifact = analyze_rollouts(
            materialized,
            _rows(materialized, phase="development"),
            runner_plan=_runner_plan(materialized, phase="development"),
            expected_protocol_sha256=materialized["protocol_sha256"],
            phase="development",
            candidate_id=CANDIDATE,
        )
        self.assertFalse(artifact["victory_claim_allowed"])
        self.assertTrue(
            any("SEALED_FOR_EXECUTION" in reason for reason in artifact["blocked_reasons"])
        )

    def test_cross_policy_component_drift_is_rejected(self) -> None:
        materialized = materialize_protocol(_protocol(eligible=True))
        rows = _rows(materialized, phase="development")
        rows[1]["component_id"] = "wrong-component"
        rows[1]["row_sha256"] = sha256_json(
            {key: value for key, value in rows[1].items() if key != "row_sha256"}
        )
        with self.assertRaisesRegex(FuryMultiseedProtocolError, "component_id"):
            analyze_rollouts(
                materialized,
                rows,
                runner_plan=_runner_plan(materialized, phase="development"),
                expected_protocol_sha256=materialized["protocol_sha256"],
                phase="development",
                candidate_id=CANDIDATE,
            )

    def test_inconsistent_damage_and_dps_is_rejected(self) -> None:
        materialized = materialize_protocol(_protocol(eligible=True))
        rows = _rows(materialized, phase="development")
        rows[0]["damage"] = float(rows[0]["damage"]) + 1.0
        rows[0]["row_sha256"] = sha256_json(
            {key: value for key, value in rows[0].items() if key != "row_sha256"}
        )
        with self.assertRaisesRegex(FuryMultiseedProtocolError, "inconsistent"):
            analyze_rollouts(
                materialized,
                rows,
                runner_plan=_runner_plan(materialized, phase="development"),
                expected_protocol_sha256=materialized["protocol_sha256"],
                phase="development",
                candidate_id=CANDIDATE,
            )

    def test_positive_nonfaithful_reason_blocks_the_gate(self) -> None:
        materialized = materialize_protocol(_protocol(eligible=True))
        rows = _rows(
            materialized,
            phase="development",
            nonfaithful_first=True,
        )
        artifact = analyze_rollouts(
            materialized,
            rows,
            runner_plan=_runner_plan(materialized, phase="development"),
            expected_protocol_sha256=materialized["protocol_sha256"],
            phase="development",
            candidate_id=CANDIDATE,
        )
        self.assertFalse(artifact["simulator_multiseed_gate_passed"])
        self.assertTrue(any("nonfaithful" in reason for reason in artifact["blocked_reasons"]))

    def test_missing_runner_provenance_field_is_rejected(self) -> None:
        materialized = materialize_protocol(_protocol(eligible=True))
        rows = _rows(materialized, phase="development")
        rows[0].pop("catalog_sha256")
        rows[0]["row_sha256"] = sha256_json(
            {key: value for key, value in rows[0].items() if key != "row_sha256"}
        )
        with self.assertRaisesRegex(FuryMultiseedProtocolError, "catalog_sha256"):
            analyze_rollouts(
                materialized,
                rows,
                runner_plan=_runner_plan(materialized, phase="development"),
                expected_protocol_sha256=materialized["protocol_sha256"],
                phase="development",
                candidate_id=CANDIDATE,
            )

    def test_missing_paired_row_is_rejected(self) -> None:
        materialized = materialize_protocol(_protocol(eligible=True))
        rows = _rows(materialized, phase="development")
        rows.pop()
        with self.assertRaisesRegex(FuryMultiseedProtocolError, "Cartesian product"):
            analyze_rollouts(
                materialized,
                rows,
                runner_plan=_runner_plan(materialized, phase="development"),
                expected_protocol_sha256=materialized["protocol_sha256"],
                phase="development",
                candidate_id=CANDIDATE,
            )


if __name__ == "__main__":
    unittest.main()
