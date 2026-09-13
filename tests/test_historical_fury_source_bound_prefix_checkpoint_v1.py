from __future__ import annotations

from copy import deepcopy
import unittest

from o2o_dps import historical_fury_source_bound_prefix_checkpoint_v1 as checkpoint


PLAYER = "0x00000000000000ab"
TARGET = "0xF130000001000001"


def _evidence() -> dict[str, object]:
    order = [1_000, 10, 3, 4]
    return {
        "segment_ref": "sha256:" + "1" * 64,
        "source_identity": {
            "instance_id": "instance-1",
            "encounter_id": "encounter-1",
            "player_guid": PLAYER,
            "wave_id": "wave-1",
            "wave_ordinal": 1,
        },
        "execution_window_contract": {
            "bound_decision_support": {"start_order_key": order}
        },
        "bound_decision_prefix_deltas": [
            {
                "order_key": order,
                "action_key": "warrior.bloodthirst",
                "strict_prefix_state": {
                    "alive_target_guids": [TARGET],
                    "last_observed_hostile_target_guid": TARGET,
                    "focal_direct_damage_prefix_amount": 123,
                },
            }
        ],
        # These deliberately future/retrospective fields must not become HP.
        "targets": [
            {
                "target_guid": TARGET,
                "health_evidence": {
                    "exact_initial_or_max_health": None,
                    "retrospective_kill_budget_proxy": 999_999,
                },
            }
        ],
        "alive_at_last_bound_decision_health_lower_bounds": [
            {"target_guid": TARGET, "persistent_health_model_lower_bound": 888_888}
        ],
    }


def _coverage(*, aura: bool = True) -> dict[str, object]:
    return {
        stream: {
            "status": "AVAILABLE_FRAME_VERIFIED" if (stream != "aura" or aura) else "MISSING_ENCOUNTER_FRAME",
            "strict_prefix_event_count": 0,
        }
        for stream in checkpoint.STATE_STREAMS
    }


def _row(
    *,
    timestamp: int,
    event_index: int,
    stream: str,
    source_guid: str | None = None,
    target_guid: str | None = None,
    spell: str | None = None,
    spell_id: int | None = None,
    value: int | None = None,
    state_payload: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "instance": "instance-1",
        "encounter": "encounter-1",
        "timestamp_ms": timestamp,
        "event_index": event_index,
        "offset_ms": timestamp,
        "stream_type": stream,
        "source_guid": source_guid,
        "target_guid": target_guid,
        "spell": spell,
        "spell_id": spell_id,
        "value": value,
        "synthetic": False,
        "event_meta": {"event_index": event_index, "offset_ms": timestamp},
        "state_payload": state_payload or {},
        "official": {"message": {}, "message_sha256": f"{event_index + 1:064x}"},
        "provenance": {
            "frame_index": 0,
            "frame_message_index": event_index,
            "source_manifest_sha256": "2" * 64,
            "source_object_sha256": "3" * 64,
        },
    }


def _aura(
    timestamp: int,
    event_index: int,
    *,
    target: str,
    spell: str,
    spell_id: int,
    state: str,
    amount: int,
    is_buff: bool,
) -> dict[str, object]:
    return _row(
        timestamp=timestamp,
        event_index=event_index,
        stream="aura",
        target_guid=target,
        spell=spell,
        spell_id=spell_id,
        value=amount,
        state_payload={
            "target": target,
            "spell_name": spell,
            "current_amount": amount,
            "state": {"number": 2 if state == "StateRemoved" else 1, "name": state},
            "application": {"number": 1, "name": "ApplicationGains"},
            "is_buff": is_buff,
        },
    )


class PrefixCheckpointContractTests(unittest.TestCase):
    def test_separates_hp_and_retains_only_partial_prefix_state(self) -> None:
        rows = [
            _aura(
                100,
                1,
                target=PLAYER.upper(),
                spell="Berserker Stance",
                spell_id=2458,
                state="StateAdded",
                amount=1,
                is_buff=True,
            ),
            _row(
                timestamp=200,
                event_index=2,
                stream="resource_change",
                source_guid=PLAYER.upper(),
                target_guid=PLAYER.upper(),
                spell="Bloodrage",
                spell_id=2687,
                value=100,
                state_payload={
                    "amount": 100,
                    "resource_type": "Rage",
                    "direction": "Gain",
                    "over_resource": 0,
                    "source_name": "Bloodrage",
                },
            ),
        ]
        result = checkpoint.compile_checkpoint_row(
            _evidence(), state_rows=rows, stream_coverage=_coverage()
        )
        fields = result["fields"]
        self.assertIsNone(fields["target.max_health"]["value"])
        self.assertIsNone(fields["target.current_health"]["value"])
        self.assertIsNot(
            fields["target.max_health"], fields["target.current_health"]
        )
        self.assertEqual(fields["player.stance"]["value"], "BERSERKER")
        self.assertTrue(fields["player.stance"]["exact_checkpoint_equivalent"])
        self.assertIsNone(fields["player.rage_current"]["value"])
        rage_diagnostics = fields["player.rage_current"]["diagnostics"]
        self.assertEqual(rage_diagnostics["strict_prefix_rage_change_event_count"], 1)
        self.assertFalse(rage_diagnostics["change_values_integrated"])
        self.assertEqual(
            fields["player.self_auras_and_procs"]["observation_category"],
            "PARTIAL",
        )
        active = fields["player.self_auras_and_procs"]["value"]["observed_active"]
        self.assertIsNone(active[0]["remaining_duration_ms"])
        rendered = repr(result)
        self.assertNotIn("999999", rendered)
        self.assertNotIn("888888", rendered)
        self.assertFalse(result["exact_checkpoint_ready"])

    def test_state_removed_closes_aura_even_with_nonzero_reported_amount(self) -> None:
        rows = [
            _aura(
                100,
                1,
                target=PLAYER,
                spell="Berserker Stance",
                spell_id=2458,
                state="StateAdded",
                amount=1,
                is_buff=True,
            ),
            _aura(
                200,
                2,
                target=PLAYER,
                spell="Berserker Stance",
                spell_id=2458,
                state="StateRemoved",
                amount=1,
                is_buff=True,
            ),
        ]
        result = checkpoint.compile_checkpoint_row(
            _evidence(), state_rows=rows, stream_coverage=_coverage()
        )
        self.assertEqual(
            result["fields"]["player.stance"]["observation_category"], "MISSING"
        )
        self.assertEqual(
            result["fields"]["player.self_auras_and_procs"]["value"]["observed_active"],
            [],
        )

    def test_attributes_only_supported_candidate_armor_contribution(self) -> None:
        cast = _row(
            timestamp=300,
            event_index=3,
            stream="aura_cast",
            source_guid=PLAYER.upper(),
            target_guid=TARGET,
            spell="Sunder Armor",
            spell_id=11597,
            state_payload={
                "caster": PLAYER,
                "target": TARGET,
                "effect": 6,
                "effect_aura_name": 22,
                "effect_misc_value": 1,
                "duration_ms": 30_000,
                "cap_status": 0,
            },
        )
        added = _aura(
            305,
            4,
            target=TARGET,
            spell="Sunder Armor",
            spell_id=11597,
            state="StateAdded",
            amount=1,
            is_buff=False,
        )
        result = checkpoint.compile_checkpoint_row(
            _evidence(), state_rows=[cast, added], stream_coverage=_coverage()
        )
        field = result["fields"]["target.candidate_owned_existing_debuffs"]
        self.assertEqual(field["observation_category"], "PARTIAL")
        contributions = field["value"]["exact_candidate_physical_armor_contributions"]
        self.assertEqual(contributions[0]["exact_candidate_stack_count"], 1)
        self.assertIsNone(contributions[0]["remaining_duration_ms"])
        self.assertFalse(field["exact_checkpoint_equivalent"])

    def test_rejects_current_or_future_event_at_cutoff(self) -> None:
        current = _aura(
            1_000,
            10,
            target=PLAYER,
            spell="Berserker Stance",
            spell_id=2458,
            state="StateAdded",
            amount=1,
            is_buff=True,
        )
        with self.assertRaisesRegex(
            checkpoint.HistoricalFuryPrefixCheckpointV1Error,
            "not strictly before",
        ):
            checkpoint.compile_checkpoint_row(
                _evidence(), state_rows=[current], stream_coverage=_coverage()
            )

    def test_validator_rejects_default_zero_and_future_promotion(self) -> None:
        result = checkpoint.compile_checkpoint_row(
            _evidence(), state_rows=[], stream_coverage=_coverage()
        )
        defaulted = deepcopy(result)
        defaulted["fields"]["target.current_health"]["value"] = 0
        with self.assertRaisesRegex(
            checkpoint.HistoricalFuryPrefixCheckpointV1Error, "carries a value"
        ):
            checkpoint.validate_checkpoint_row(defaulted)

        future = deepcopy(result)
        future["fields"]["player.rage_current"]["future_suffix_used"] = True
        with self.assertRaisesRegex(
            checkpoint.HistoricalFuryPrefixCheckpointV1Error, "future suffix"
        ):
            checkpoint.validate_checkpoint_row(future)

    def test_missing_aura_frame_fails_fields_closed(self) -> None:
        result = checkpoint.compile_checkpoint_row(
            _evidence(), state_rows=[], stream_coverage=_coverage(aura=False)
        )
        self.assertEqual(
            result["fields"]["player.stance"]["observation_status"],
            "MISSING_AURA_STREAM_FRAME",
        )
        self.assertIsNone(result["fields"]["player.self_auras_and_procs"]["value"])

    def test_manifest_binds_real_fieldwise_coverage(self) -> None:
        row = checkpoint.compile_checkpoint_row(
            _evidence(), state_rows=[], stream_coverage=_coverage()
        )
        manifest = checkpoint.build_manifest(
            rows=[row],
            evidence_manifest={
                "schema": "historical_fury_source_bound_environment_evidence/v1",
                "implementation_revision": "fixture",
                "content_address": {"sha256": "4" * 64},
            },
            raw_input={
                "schema": "chronicle_external_api_ingest/v1",
                "manifest_sha256": "5" * 64,
                "network_request_count": 0,
            },
        )
        summary = manifest["summary"]
        self.assertEqual(summary["checkpoint_row_count"], 1)
        self.assertEqual(summary["exact_checkpoint_ready_row_count"], 0)
        self.assertEqual(
            summary["coverage_by_field"]["target.max_health"],
            {
                "exact_observed_row_count": 0,
                "partial_evidence_row_count": 0,
                "missing_or_ambiguous_row_count": 1,
                "row_count": 1,
            },
        )
        interface = manifest["simulator_interface_audit"]
        self.assertEqual(
            "wowsims-turtle load_dynamic_v4 + RaidSimRequest",
            interface["audited_interface"],
        )
        self.assertTrue(
            interface["dynamic_v4_target_health"][
                "max_health_separate_from_current_health"
            ]
        )
        self.assertTrue(
            interface["dynamic_v4_target_health"][
                "source_evidence_still_required_for_both_health_fields"
            ]
        )
        self.assertFalse(interface["exact_checkpoint_wire_supported"])
        checkpoint.validate_manifest(manifest)


if __name__ == "__main__":
    unittest.main()
