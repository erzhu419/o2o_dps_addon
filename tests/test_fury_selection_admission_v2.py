from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlencode

from o2o_dps.fury_selection_admission_v2 import (
    CORPUS_IDENTITY_SCHEMA,
    COMPONENT_IDENTITY_SCHEMA,
    DISCOVERY_CAPTURE_MANIFEST_SCHEMA,
    DISCOVERY_COMPLETENESS_BOUNDARY,
    DISCOVERY_ENDPOINT,
    DISCOVERY_ORDER,
    DISCOVERY_QUERY_SCHEMA,
    DISCOVERY_UNIVERSE_SCHEMA,
    INSTANCE_EVIDENCE_MANIFEST_SCHEMA,
    RUNNER_IDENTITY_SCHEMA,
    SEED_IDENTITY_SCHEMA,
    FurySelectionAdmissionV2Error,
    build_candidate_seal,
    build_development_exclusion_snapshot,
    build_external_anchor_seal,
    build_final_discovery_query,
    build_final_corpus_admission_receipt,
    build_frozen_selection_shortlist,
    build_selection_receipt,
    canonical_json_bytes,
    replay_final_discovery_capture,
    sha256_json,
    validate_candidate_seal,
    validate_final_corpus_admission_receipt,
    validate_frozen_selection_shortlist,
    validate_selection_receipt,
)


DEV_INSTANCE_ID = "00000000-0000-4000-8000-000000000001"
COMPONENT_OVERLAP_INSTANCE_ID = "00000000-0000-4000-8000-000000000002"
INSTANCE_A = "00000000-0000-4000-8000-000000000003"
INSTANCE_B = "00000000-0000-4000-8000-000000000004"
INSTANCE_C = "00000000-0000-4000-8000-000000000005"
REALM_ID = "00000000-0000-4000-8000-000000000010"
SERVER_ID = "00000000-0000-4000-8000-000000000011"
DEV_GUILD_ID = "00000000-0000-4000-8000-000000000012"
GUILD_A = "00000000-0000-4000-8000-000000000013"
GUILD_B = "00000000-0000-4000-8000-000000000014"
GUILD_C = "00000000-0000-4000-8000-000000000015"

_TEMP_DIRS: list[tempfile.TemporaryDirectory[str]] = []


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _candidate(label: str) -> dict[str, object]:
    return {
        "policy_id": f"candidate.{label}",
        "policy_source_sha256": _digest(f"{label}:source"),
        "policy_adapter_sha256": _digest(f"{label}:adapter"),
        "policy_profile_sha256": _digest(f"{label}:profile"),
    }


def _seed_identity(phase: str) -> dict[str, object]:
    return {
        "schema": SEED_IDENTITY_SCHEMA,
        "phase": phase,
        "seed_count": 256 if phase == "selection_validation" else 1000,
        "seed_list_sha256": _digest(f"{phase}:seeds"),
    }


def _corpus_identity(
    label: str,
    *,
    source_instance_provenance_sha256: str | None = None,
) -> dict[str, object]:
    return {
        "schema": CORPUS_IDENTITY_SCHEMA,
        "corpus_manifest_sha256": _digest(f"{label}:manifest"),
        "corpus_binding_sha256": _digest(f"{label}:binding"),
        "source_instance_provenance_sha256": (
            source_instance_provenance_sha256
            or _digest(f"{label}:source-instance-provenance")
        ),
        "scenario_count": 24,
    }


def _runner_identity(label: str) -> dict[str, object]:
    return {
        "schema": RUNNER_IDENTITY_SCHEMA,
        "runner_source_identity_sha256": _digest(f"{label}:source-closure"),
        "runner_inputs_sha256": _digest(f"{label}:runner-inputs"),
        "runner_scenario_bundle_sha256": _digest(f"{label}:scenario-bundle"),
        "bridge_sha256": _digest(f"{label}:bridge"),
        "execution_bundle_sha256": _digest(f"{label}:execution-bundle"),
    }


def _shortlist() -> dict[str, object]:
    return build_frozen_selection_shortlist(
        candidates=[_candidate("b"), _candidate("a")],
        metric_id="paired_mean_improvement_pct",
        metric_definition_sha256=_digest("metric-definition"),
        metric_direction="maximize",
        selection_once_nonce_sha256=_digest("selection-once-nonce"),
        seed_identity=_seed_identity("selection_validation"),
        corpus_identity=_corpus_identity("selection"),
        runner_identity=_runner_identity("selection"),
    )


def _selection_receipt(*, tied: bool = False) -> dict[str, object]:
    shortlist = _shortlist()
    candidates = shortlist["candidates"]
    assert isinstance(candidates, list)
    return build_selection_receipt(
        shortlist,
        metric_rows=[
            {
                "candidate_identity_sha256": candidate["candidate_identity_sha256"],
                "metric_value": 4.0 if tied else float(index + 1),
                "metric_evidence_sha256": _digest(
                    f"metric-evidence-{candidate['candidate_identity_sha256']}"
                ),
            }
            for index, candidate in enumerate(reversed(candidates))
        ],
    )


def _candidate_seal() -> tuple[dict[str, object], str]:
    selection = _selection_receipt()
    anchor = build_external_anchor_seal(
        anchored_payload_sha256=selection["selection_receipt_sha256"],
        anchored_at="2026-09-10T00:00:00Z",
        anchor_provider="TEST_FIXTURE_NOT_A_REAL_TRUSTED_PROVIDER",
        anchor_reference="test-only://selection/1",
        anchor_evidence_sha256=_digest("test-only-anchor-evidence"),
    )
    return build_candidate_seal(selection, anchor), anchor["external_anchor_seal_sha256"]


def _activity(
    instance_id: str,
    uploaded_at: str,
    guild_id: str,
    *,
    instance_name: str = "Upper Tower of Karazhan",
    complete: bool = True,
) -> dict[str, object]:
    return {
        "id": instance_id,
        "slug": f"fixture-{instance_id.lower()}",
        "name": instance_name,
        "realm": {
            "id": REALM_ID,
            "server_id": SERVER_ID,
            "name": "Turtle WoW",
        },
        "guild": {"id": guild_id, "name": f"Guild {guild_id[-2:]}"},
        "uploaded_at": uploaded_at,
        "started_at": "2026-09-01T00:00:00Z",
        "ended_at": "2026-09-01T01:00:00Z",
        "player_count": 2,
        "boss_count": 9 if complete else 8,
        "boss_kills": 9 if complete else 8,
        "has_youtube_video": False,
    }


def _component_hash(*, kind: str, identity: str) -> str:
    if kind == "GUILD":
        row = {
            "schema": COMPONENT_IDENTITY_SCHEMA,
            "kind": "GUILD",
            "realm_id": REALM_ID,
            "guild_id": identity,
        }
    else:
        row = {
            "schema": COMPONENT_IDENTITY_SCHEMA,
            "kind": "PLAYER",
            "realm_id": REALM_ID,
            "player_guid": identity.casefold(),
        }
    return sha256_json(row)


def _write_json(path: Path, value: object) -> None:
    path.write_bytes(canonical_json_bytes(value))


def _new_fixture_root() -> Path:
    owner = tempfile.TemporaryDirectory(prefix="fury-final-discovery-")
    _TEMP_DIRS.append(owner)
    return Path(owner.name)


def _instance_evidence(
    root: Path,
    activity: dict[str, object],
    *,
    position: int,
) -> Path:
    instance_id = str(activity["id"])
    guild = activity["guild"]
    assert isinstance(guild, dict)
    complete = int(activity["boss_count"]) == 9
    metadata_path = root / f"instance-{position}-metadata.json"
    rankings_path = root / f"instance-{position}-rankings.json"
    activity_path = root / f"instance-{position}-all-activity.bin"
    evidence_path = root / f"instance-{position}-evidence.json"
    encounters = [
        {
            "id": f"encounter-{position}-{index}",
            "boss": True,
            "kill_type": "clean",
        }
        for index in range(9 if complete else 8)
    ]
    _write_json(
        metadata_path,
        {
            "id": instance_id,
            "realm_id": REALM_ID,
            "name": activity["name"],
            "guild": guild,
            "players": {
                f"Player-{position:08d}": {"name": f"Player {position}"},
                f"Player-{position + 100:08d}": {
                    "name": f"Player {position + 100}"
                },
            },
            "encounters": encounters,
        },
    )
    _write_json(
        rankings_path,
        [{"player_guid": f"Player-{position:08d}", "dps": 1000 + position}],
    )
    activity_path.write_bytes(f"fixture-all-activity-{instance_id}".encode("utf-8"))
    _write_json(
        evidence_path,
        {
            "schema": INSTANCE_EVIDENCE_MANIFEST_SCHEMA,
            "instance_id": instance_id,
            "instance_metadata_path": metadata_path.name,
            "ranking_records_path": rankings_path.name,
            "all_activity_stream_path": activity_path.name,
        },
    )
    return evidence_path


class _PhysicalDiscovery:
    def __init__(
        self,
        query: dict[str, object],
        activities: list[dict[str, object]],
    ) -> None:
        self.root = _new_fixture_root()
        self.query = query
        self.activities = activities
        page_size = int(query["page_size"])
        self.response_paths: list[Path] = []
        self.evidence_paths: list[Path] = []
        for index, activity in enumerate(activities):
            self.evidence_paths.append(
                _instance_evidence(self.root, activity, position=index + 1)
            )
        page_entries: list[dict[str, object]] = []
        for offset in range(0, len(activities), page_size):
            page_index = offset // page_size
            provider_page = page_index + 1
            page_activities = activities[offset : offset + page_size]
            has_more = offset + page_size < len(activities)
            response_path = self.root / f"recent-page-{provider_page}.json"
            _write_json(
                response_path,
                {
                    "activities": page_activities,
                    "pagination": {
                        "page": provider_page,
                        "page_size": page_size,
                        "has_more": has_more,
                    },
                },
            )
            self.response_paths.append(response_path)
            parameters = urlencode(
                {
                    "upload_after": query["uploaded_after_exclusive"],
                    "instance_name": query["instance_name"],
                    "page": provider_page,
                    "page_size": page_size,
                }
            )
            page_entries.append(
                {
                    "request_target": f"{DISCOVERY_ENDPOINT}?{parameters}",
                    "response_path": response_path.name,
                    "instance_evidence_manifest_paths": [
                        path.name
                        for path in self.evidence_paths[offset : offset + page_size]
                    ],
                }
            )
        self.manifest_path = self.root / "capture-manifest.json"
        _write_json(
            self.manifest_path,
            {
                "schema": DISCOVERY_CAPTURE_MANIFEST_SCHEMA,
                "query_sha256": sha256_json(query),
                "retrieved_at": "2026-09-20T00:00:01Z",
                "pages": page_entries,
            },
        )
        self.universe = replay_final_discovery_capture(
            query,
            candidate_frozen_at="2026-09-10T00:00:00Z",
            discovery_capture_manifest_path=self.manifest_path,
        )

    def rewrite_manifest(self, mutate: object) -> None:
        value = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        assert isinstance(value, dict)
        assert callable(mutate)
        mutate(value)
        _write_json(self.manifest_path, value)


def _source_instance_provenance_sha256(records: list[dict[str, object]]) -> str:
    entries = [
        {
            "instance_id": str(record["instance_id"]),
            "recent_activity_sha256": record["recent_activity_sha256"],
            "compact_instance_evidence_manifest_sha256": record[
                "compact_instance_evidence_manifest_sha256"
            ],
            "instance_metadata_sha256": record["instance_metadata_sha256"],
            "ranking_records_sha256": record["ranking_records_sha256"],
            "all_activity_stream_sha256": record["all_activity_stream_sha256"],
            "guild_player_component_sha256s": record[
                "guild_player_component_sha256s"
            ],
        }
        for record in records
    ]
    entries.sort(key=lambda row: row["instance_id"])
    return sha256_json(entries)


def _final_corpus_identity(
    universe: dict[str, object], selected_instance_ids: list[str]
) -> dict[str, object]:
    selected = {instance_id.lower() for instance_id in selected_instance_ids}
    records = [
        record
        for page in universe["pages"]
        for record in page["records"]
        if str(record["instance_id"]).lower() in selected
    ]
    return _corpus_identity(
        "final",
        source_instance_provenance_sha256=(
            _source_instance_provenance_sha256(records)
        ),
    )


def _query(*, take_first_n: int = 2) -> dict[str, object]:
    return build_final_discovery_query(
        candidate_frozen_at="2026-09-10T00:00:00Z",
        instance_name="Upper Tower of Karazhan",
        uploaded_before_inclusive="2026-09-20T00:00:00Z",
        page_size=2,
        take_first_n=take_first_n,
    )


def _universe(
    query: dict[str, object],
    *,
    activities: list[dict[str, object]] | None = None,
) -> _PhysicalDiscovery:
    if activities is None:
        activities = [
            _activity(INSTANCE_C, "2026-09-13T00:00:00Z", GUILD_C),
            _activity(INSTANCE_B, "2026-09-12T00:00:00Z", GUILD_B),
            _activity(INSTANCE_A, "2026-09-12T00:00:00Z", GUILD_A),
            _activity(
                COMPONENT_OVERLAP_INSTANCE_ID,
                "2026-09-11T00:00:00Z",
                DEV_GUILD_ID,
            ),
        ]
    return _PhysicalDiscovery(query, activities)


def _development_exclusion() -> dict[str, object]:
    return build_development_exclusion_snapshot(
        [
            {
                "instance_id": DEV_INSTANCE_ID,
                "guild_player_component_sha256s": [
                    _component_hash(kind="GUILD", identity=DEV_GUILD_ID)
                ],
            }
        ]
    )


def _final_receipt() -> tuple[dict[str, object], str, _PhysicalDiscovery]:
    candidate, anchor_hash = _candidate_seal()
    query = _query()
    physical = _universe(query)
    selected_instance_ids = [INSTANCE_A, INSTANCE_B]
    receipt = build_final_corpus_admission_receipt(
        candidate_seal=candidate,
        trusted_external_anchor_seal_sha256s=[anchor_hash],
        discovery_query=query,
        discovery_capture_manifest_path=physical.manifest_path,
        development_exclusion_snapshot=_development_exclusion(),
        selected_instance_ids=selected_instance_ids,
        final_seed_identity=_seed_identity("final_confirmation"),
        final_corpus_identity=_final_corpus_identity(
            physical.universe, selected_instance_ids
        ),
        final_runner_identity=_runner_identity("final"),
    )
    return receipt, anchor_hash, physical


class FrozenShortlistTests(unittest.TestCase):
    def test_shortlist_carries_full_candidate_and_input_identities(self) -> None:
        shortlist = validate_frozen_selection_shortlist(_shortlist())
        self.assertEqual(shortlist["status"], "FROZEN")
        self.assertEqual(shortlist["candidate_count"], 2)
        self.assertEqual(shortlist["selection_contract"]["max_selected"], 1)
        self.assertEqual(
            set(shortlist["candidates"][0]),
            {
                "policy_id",
                "policy_source_sha256",
                "policy_adapter_sha256",
                "policy_profile_sha256",
                "candidate_identity_sha256",
            },
        )
        self.assertEqual(
            set(shortlist["selection_input_identities"]),
            {"seed", "corpus", "runner"},
        )

    def test_missing_adapter_identity_is_rejected(self) -> None:
        candidate = _candidate("bad")
        del candidate["policy_adapter_sha256"]
        with self.assertRaisesRegex(FurySelectionAdmissionV2Error, "fields mismatch"):
            build_frozen_selection_shortlist(
                candidates=[candidate],
                metric_id="metric",
                metric_definition_sha256=_digest("metric"),
                metric_direction="maximize",
                selection_once_nonce_sha256=_digest("nonce"),
                seed_identity=_seed_identity("selection_validation"),
                corpus_identity=_corpus_identity("selection"),
                runner_identity=_runner_identity("selection"),
            )

    def test_alternate_rule_or_multiple_winners_cannot_be_injected(self) -> None:
        for field, value in (("rule", {"rule_id": "post_hoc"}), ("max_selected", 2)):
            attacked = copy.deepcopy(_shortlist())
            attacked["selection_contract"][field] = value
            with self.assertRaises(FurySelectionAdmissionV2Error):
                validate_frozen_selection_shortlist(attacked)


class SelectionReceiptTests(unittest.TestCase):
    def test_receipt_covers_complete_shortlist_and_deterministic_tie_break(self) -> None:
        receipt = validate_selection_receipt(_selection_receipt(tied=True))
        ranked = receipt["ranked_candidate_identity_sha256s"]
        self.assertEqual(ranked, sorted(ranked))
        self.assertEqual(
            receipt["selected_candidates"][0]["candidate_identity_sha256"],
            ranked[0],
        )
        self.assertEqual(
            receipt["selection_once_nonce_sha256"],
            receipt["frozen_shortlist"]["selection_contract"][
                "selection_once_nonce_sha256"
            ],
        )

    def test_missing_or_extra_shortlist_result_is_rejected(self) -> None:
        shortlist = _shortlist()
        candidate_hash = shortlist["candidates"][0]["candidate_identity_sha256"]
        row = {
            "candidate_identity_sha256": candidate_hash,
            "metric_value": 1.0,
            "metric_evidence_sha256": _digest("evidence"),
        }
        with self.assertRaisesRegex(FurySelectionAdmissionV2Error, "complete shortlist"):
            build_selection_receipt(shortlist, metric_rows=[row])
        attacked = dict(row)
        attacked["candidate_identity_sha256"] = _digest("not-shortlisted")
        with self.assertRaisesRegex(FurySelectionAdmissionV2Error, "complete shortlist"):
            build_selection_receipt(shortlist, metric_rows=[row, attacked])

    def test_metric_or_receipt_tamper_is_rejected(self) -> None:
        receipt = _selection_receipt()
        receipt["complete_metric_rows"][0]["metric_value"] = 999.0
        with self.assertRaisesRegex(FurySelectionAdmissionV2Error, "not content-addressed"):
            validate_selection_receipt(receipt)


class CandidateSealTests(unittest.TestCase):
    def test_frozen_at_is_derived_from_the_anchored_selection_receipt(self) -> None:
        candidate, anchor_hash = _candidate_seal()
        validated = validate_candidate_seal(
            candidate,
            trusted_external_anchor_seal_sha256s=[anchor_hash],
        )
        self.assertEqual(validated["frozen_at"], "2026-09-10T00:00:00.000000Z")
        self.assertEqual(
            validated["external_anchor_seal"]["anchored_payload_sha256"],
            validated["selection_receipt"]["selection_receipt_sha256"],
        )

    def test_self_authored_anchor_is_not_trusted_by_its_own_fields(self) -> None:
        candidate, _ = _candidate_seal()
        with self.assertRaisesRegex(FurySelectionAdmissionV2Error, "out-of-band"):
            validate_candidate_seal(
                candidate,
                trusted_external_anchor_seal_sha256s=[],
            )
        candidate["external_anchor_seal"]["trusted"] = True
        with self.assertRaisesRegex(FurySelectionAdmissionV2Error, "fields mismatch"):
            validate_candidate_seal(
                candidate,
                trusted_external_anchor_seal_sha256s=[_digest("invented")],
            )

    def test_anchor_payload_or_frozen_at_tamper_is_rejected(self) -> None:
        selection = _selection_receipt()
        wrong_anchor = build_external_anchor_seal(
            anchored_payload_sha256=_digest("some-other-payload"),
            anchored_at="2026-09-10T00:00:00Z",
            anchor_provider="TEST_ONLY",
            anchor_reference="test-only://wrong",
            anchor_evidence_sha256=_digest("wrong-evidence"),
        )
        with self.assertRaisesRegex(FurySelectionAdmissionV2Error, "does not anchor"):
            build_candidate_seal(selection, wrong_anchor)
        candidate, anchor_hash = _candidate_seal()
        candidate["frozen_at"] = "2026-01-01T00:00:00.000000Z"
        with self.assertRaisesRegex(FurySelectionAdmissionV2Error, "not content-addressed"):
            validate_candidate_seal(
                candidate,
                trusted_external_anchor_seal_sha256s=[anchor_hash],
            )


class FinalCorpusAdmissionTests(unittest.TestCase):
    def test_instance_ids_are_canonical_uuids_and_case_aliases_collapse(self) -> None:
        with self.assertRaisesRegex(FurySelectionAdmissionV2Error, "canonical UUID"):
            build_development_exclusion_snapshot(
                [
                    {
                        "instance_id": "not-a-uuid",
                        "guild_player_component_sha256s": [_digest("component")],
                    }
                ]
            )

        query = _query(take_first_n=1)
        with self.assertRaisesRegex(FurySelectionAdmissionV2Error, "duplicate instance IDs"):
            _universe(
                query,
                activities=[
                    _activity(
                        COMPONENT_OVERLAP_INSTANCE_ID,
                        "2026-09-11T00:00:00Z",
                        DEV_GUILD_ID,
                    ),
                    _activity(
                        COMPONENT_OVERLAP_INSTANCE_ID.upper(),
                        "2026-09-12T00:00:00Z",
                        GUILD_A,
                    ),
                ],
            )

    def test_first_n_replay_excludes_development_component_overlap(self) -> None:
        receipt, anchor_hash, physical = _final_receipt()
        validated = validate_final_corpus_admission_receipt(
            receipt,
            trusted_external_anchor_seal_sha256s=[anchor_hash],
            discovery_capture_manifest_path=physical.manifest_path,
        )
        selected = validated["selection_replay"]["selected_instances"]
        self.assertEqual(
            [row["instance_id"] for row in selected],
            [INSTANCE_A, INSTANCE_B],
        )
        overlap = next(
            row
            for row in validated["eligibility_ledger"]
            if row["instance"]["instance_id"]
            == COMPONENT_OVERLAP_INSTANCE_ID
        )
        self.assertFalse(overlap["eligible"])
        self.assertIn(
            "DEVELOPMENT_GUILD_PLAYER_COMPONENT_OVERLAP",
            overlap["exclusion_reasons"],
        )
        self.assertEqual(
            set(validated["final_input_identities"]), {"seed", "corpus", "runner"}
        )
        provenance = validated["selected_source_instance_provenance"]
        self.assertEqual(provenance["order"], "instance_id_ascending")
        self.assertEqual(
            validated["final_input_identities"]["corpus"][
                "source_instance_provenance_sha256"
            ],
            provenance["source_instance_provenance_sha256"],
        )
        self.assertEqual(
            validated["complete_discovery_universe"]["completeness_boundary"],
            DISCOVERY_COMPLETENESS_BOUNDARY,
        )
        first_page = validated["complete_discovery_universe"]["pages"][0]
        self.assertEqual(
            first_page["raw_response_sha256"],
            hashlib.sha256(physical.response_paths[0].read_bytes()).hexdigest(),
        )
        first_instance = next(
            row
            for page in validated["complete_discovery_universe"]["pages"]
            for row in page["records"]
            if row["instance_id"] == COMPONENT_OVERLAP_INSTANCE_ID
        )
        self.assertIn(
            _component_hash(kind="GUILD", identity=DEV_GUILD_ID),
            first_instance["guild_player_component_sha256s"],
        )
        self.assertNotIn("response_path", json.dumps(validated))

    def test_skipping_an_earlier_eligible_instance_is_rejected(self) -> None:
        candidate, anchor_hash = _candidate_seal()
        query = _query()
        physical = _universe(query)
        proposed = [INSTANCE_B, INSTANCE_C]
        with self.assertRaisesRegex(FurySelectionAdmissionV2Error, "earlier instance was skipped"):
            build_final_corpus_admission_receipt(
                candidate_seal=candidate,
                trusted_external_anchor_seal_sha256s=[anchor_hash],
                discovery_query=query,
                discovery_capture_manifest_path=physical.manifest_path,
                development_exclusion_snapshot=_development_exclusion(),
                selected_instance_ids=proposed,
                final_seed_identity=_seed_identity("final_confirmation"),
                final_corpus_identity=_final_corpus_identity(
                    physical.universe, proposed
                ),
                final_runner_identity=_runner_identity("final"),
            )

    def test_development_instance_id_case_alias_is_derived_and_excluded(self) -> None:
        candidate, anchor_hash = _candidate_seal()
        query = _query(take_first_n=1)
        physical = _universe(
            query,
            activities=[
                _activity(INSTANCE_C, "2026-09-13T00:00:00Z", GUILD_C),
                _activity(INSTANCE_B, "2026-09-12T00:00:00Z", GUILD_B),
                _activity(INSTANCE_A, "2026-09-12T00:00:00Z", GUILD_A),
                _activity(
                    DEV_INSTANCE_ID.upper(),
                    "2026-09-11T00:00:00Z",
                    GUILD_C,
                ),
            ],
        )
        selected_instance_ids = [INSTANCE_A.upper()]
        receipt = build_final_corpus_admission_receipt(
            candidate_seal=candidate,
            trusted_external_anchor_seal_sha256s=[anchor_hash],
            discovery_query=query,
            discovery_capture_manifest_path=physical.manifest_path,
            development_exclusion_snapshot=_development_exclusion(),
            selected_instance_ids=selected_instance_ids,
            final_seed_identity=_seed_identity("final_confirmation"),
            final_corpus_identity=_final_corpus_identity(
                physical.universe, selected_instance_ids
            ),
            final_runner_identity=_runner_identity("final"),
        )
        dev = next(
            row for row in receipt["eligibility_ledger"]
            if row["instance"]["instance_id"] == DEV_INSTANCE_ID
        )
        self.assertIn("DEVELOPMENT_INSTANCE_OVERLAP", dev["exclusion_reasons"])
        self.assertEqual(
            receipt["selection_replay"]["selected_instances"][0]["instance_id"],
            INSTANCE_A,
        )

    def test_in_memory_discovery_records_and_hashes_are_rejected(self) -> None:
        candidate, anchor_hash = _candidate_seal()
        query = _query()
        with self.assertRaisesRegex(
            FurySelectionAdmissionV2Error, "caller-provided discovery"
        ):
            build_final_corpus_admission_receipt(
                candidate_seal=candidate,
                trusted_external_anchor_seal_sha256s=[anchor_hash],
                discovery_query=query,
                discovery_universe={
                    "raw_response_sha256": _digest("self-reported"),
                    "records": [],
                },
                development_exclusion_snapshot=_development_exclusion(),
                selected_instance_ids=[INSTANCE_A, INSTANCE_B],
                final_seed_identity=_seed_identity("final_confirmation"),
                final_corpus_identity=_corpus_identity("final"),
                final_runner_identity=_runner_identity("final"),
            )

    def test_provider_response_bytes_are_rehashed_during_validation(self) -> None:
        receipt, trusted_hash, physical = _final_receipt()
        physical.response_paths[0].write_bytes(
            physical.response_paths[0].read_bytes() + b" "
        )
        with self.assertRaisesRegex(
            FurySelectionAdmissionV2Error, "not content-addressed|provenance ledger"
        ):
            validate_final_corpus_admission_receipt(
                receipt,
                trusted_external_anchor_seal_sha256s=[trusted_hash],
                discovery_capture_manifest_path=physical.manifest_path,
            )

    def test_provider_page_order_is_parsed_from_physical_response(self) -> None:
        query = _query()
        physical = _universe(query)
        response = json.loads(
            physical.response_paths[0].read_text(encoding="utf-8")
        )
        response["activities"] = list(reversed(response["activities"]))
        _write_json(physical.response_paths[0], response)
        with self.assertRaisesRegex(FurySelectionAdmissionV2Error, "provider order"):
            replay_final_discovery_capture(
                query,
                candidate_frozen_at="2026-09-10T00:00:00Z",
                discovery_capture_manifest_path=physical.manifest_path,
            )

    def test_instance_artifact_hashes_and_components_cannot_be_self_reported(self) -> None:
        query = _query()
        physical = _universe(query)
        manifest_path = physical.evidence_paths[0]
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["instance_metadata_sha256"] = _digest("self-reported-metadata")
        manifest["guild_player_component_sha256s"] = [_digest("self-reported-player")]
        _write_json(manifest_path, manifest)
        with self.assertRaisesRegex(FurySelectionAdmissionV2Error, "fields mismatch"):
            replay_final_discovery_capture(
                query,
                candidate_frozen_at="2026-09-10T00:00:00Z",
                discovery_capture_manifest_path=physical.manifest_path,
            )

    def test_instance_artifact_replacement_is_detected_on_receipt_validation(self) -> None:
        receipt, anchor_hash, physical = _final_receipt()
        evidence = json.loads(physical.evidence_paths[2].read_text(encoding="utf-8"))
        rankings_path = physical.evidence_paths[2].parent / evidence[
            "ranking_records_path"
        ]
        rankings_path.write_bytes(rankings_path.read_bytes() + b" ")
        with self.assertRaises(FurySelectionAdmissionV2Error):
            validate_final_corpus_admission_receipt(
                receipt,
                trusted_external_anchor_seal_sha256s=[anchor_hash],
                discovery_capture_manifest_path=physical.manifest_path,
            )

    def test_receipt_validation_without_physical_capture_fails_closed(self) -> None:
        receipt, anchor_hash, _ = _final_receipt()
        with self.assertRaisesRegex(
            FurySelectionAdmissionV2Error, "retained physical"
        ):
            validate_final_corpus_admission_receipt(
                receipt,
                trusted_external_anchor_seal_sha256s=[anchor_hash],
            )

    def test_final_corpus_identity_must_match_selected_source_provenance(self) -> None:
        candidate, anchor_hash = _candidate_seal()
        query = _query()
        physical = _universe(query)
        with self.assertRaisesRegex(
            FurySelectionAdmissionV2Error, "source-instance provenance ledger"
        ):
            build_final_corpus_admission_receipt(
                candidate_seal=candidate,
                trusted_external_anchor_seal_sha256s=[anchor_hash],
                discovery_query=query,
                discovery_capture_manifest_path=physical.manifest_path,
                development_exclusion_snapshot=_development_exclusion(),
                selected_instance_ids=[INSTANCE_A, INSTANCE_B],
                final_seed_identity=_seed_identity("final_confirmation"),
                final_corpus_identity=_corpus_identity("wrong-final-provenance"),
                final_runner_identity=_runner_identity("final"),
            )

    def test_naked_discovery_hash_is_rejected(self) -> None:
        candidate, anchor_hash = _candidate_seal()
        with self.assertRaisesRegex(FurySelectionAdmissionV2Error, "caller-provided"):
            build_final_corpus_admission_receipt(
                candidate_seal=candidate,
                trusted_external_anchor_seal_sha256s=[anchor_hash],
                discovery_query=_query(take_first_n=1),
                discovery_universe={"universe_sha256": _digest("naked-universe")},
                development_exclusion_snapshot=_development_exclusion(),
                selected_instance_ids=[INSTANCE_A],
                final_seed_identity=_seed_identity("final_confirmation"),
                final_corpus_identity=_corpus_identity("final"),
                final_runner_identity=_runner_identity("final"),
            )

    def test_open_window_or_incomplete_pagination_is_rejected(self) -> None:
        candidate, anchor_hash = _candidate_seal()
        query = _query()
        early = _universe(query)
        early.rewrite_manifest(
            lambda value: value.__setitem__(
                "retrieved_at", "2026-09-19T23:59:59Z"
            )
        )
        with self.assertRaisesRegex(FurySelectionAdmissionV2Error, "before the closed window"):
            build_final_corpus_admission_receipt(
                candidate_seal=candidate,
                trusted_external_anchor_seal_sha256s=[anchor_hash],
                discovery_query=query,
                discovery_capture_manifest_path=early.manifest_path,
                development_exclusion_snapshot=_development_exclusion(),
                selected_instance_ids=[INSTANCE_A, INSTANCE_B],
                final_seed_identity=_seed_identity("final_confirmation"),
                final_corpus_identity=_corpus_identity("final"),
                final_runner_identity=_runner_identity("final"),
            )
        broken = _universe(query)
        response = json.loads(broken.response_paths[1].read_text(encoding="utf-8"))
        response["pagination"]["page"] = 99
        _write_json(broken.response_paths[1], response)
        with self.assertRaisesRegex(FurySelectionAdmissionV2Error, "not contiguous"):
            build_final_corpus_admission_receipt(
                candidate_seal=candidate,
                trusted_external_anchor_seal_sha256s=[anchor_hash],
                discovery_query=query,
                discovery_capture_manifest_path=broken.manifest_path,
                development_exclusion_snapshot=_development_exclusion(),
                selected_instance_ids=[INSTANCE_A, INSTANCE_B],
                final_seed_identity=_seed_identity("final_confirmation"),
                final_corpus_identity=_corpus_identity("final"),
                final_runner_identity=_runner_identity("final"),
            )

    def test_window_start_must_equal_candidate_seal_time(self) -> None:
        candidate, anchor_hash = _candidate_seal()
        query = _query()
        physical = _universe(query)
        query["uploaded_after_exclusive"] = "2026-09-11T00:00:00Z"
        with self.assertRaisesRegex(FurySelectionAdmissionV2Error, "candidate frozen_at"):
            build_final_corpus_admission_receipt(
                candidate_seal=candidate,
                trusted_external_anchor_seal_sha256s=[anchor_hash],
                discovery_query=query,
                discovery_capture_manifest_path=physical.manifest_path,
                development_exclusion_snapshot=_development_exclusion(),
                selected_instance_ids=[INSTANCE_A, INSTANCE_B],
                final_seed_identity=_seed_identity("final_confirmation"),
                final_corpus_identity=_corpus_identity("final"),
                final_runner_identity=_runner_identity("final"),
            )

    def test_final_runner_or_receipt_tamper_is_rejected(self) -> None:
        receipt, anchor_hash, physical = _final_receipt()
        receipt["final_input_identities"]["runner"]["bridge_sha256"] = _digest(
            "substituted-bridge"
        )
        with self.assertRaisesRegex(FurySelectionAdmissionV2Error, "not content-addressed"):
            validate_final_corpus_admission_receipt(
                receipt,
                trusted_external_anchor_seal_sha256s=[anchor_hash],
                discovery_capture_manifest_path=physical.manifest_path,
            )


if __name__ == "__main__":
    unittest.main()
