from __future__ import annotations

from collections import Counter
from copy import deepcopy
import unittest
from unittest.mock import patch

from o2o_dps import historical_fury_source_bound_dynamic_hypothesis_v2 as hypothesis_v2
from o2o_dps import historical_fury_source_bound_prefix_checkpoint_v1 as checkpoint_v1
from o2o_dps import historical_fury_source_bound_prototype_smoke_runner_v1 as smoke_v1
from o2o_dps import historical_fury_source_bound_prototype_smoke_runner_v2 as smoke_v2
from o2o_dps import historical_fury_source_bound_prototype_smoke_runner_v3 as smoke_v3


SEGMENT_REF = hypothesis_v2.READY_SEGMENT_REF
PROTOTYPE_ID = "stable_repeat_player_727acf47884bdbda"
SEED = 3321958692537367122


def _exact_health_manifest(
    *, health_source: str = "chronicle.external.core.strict_prefix"
) -> dict:
    manifest, _ = smoke_v1._strict_json_file(
        smoke_v3.DEFAULT_PREFIX_CHECKPOINT, "prefix checkpoint"
    )
    source_row = next(
        row for row in manifest["rows"] if row["segment_ref"] == SEGMENT_REF
    )
    row = deepcopy(source_row)
    for name, value in (
        ("target.max_health", 999.0),
        ("target.current_health", 777.0),
    ):
        field = row["fields"][name]
        field.update(
            {
                "observation_category": "EXACT",
                "observation_status": "EXACT_TEST_PREFIX_HEALTH_SNAPSHOT",
                "value": value,
                "source": health_source,
                "reason": None,
                "default_value_used": False,
                "future_suffix_used": False,
                "exact_checkpoint_equivalent": True,
            }
        )
    categories = Counter(
        field["observation_category"] for field in row["fields"].values()
    )
    blockers = [
        name
        for name in checkpoint_v1.REQUIRED_FIELDS
        if not row["fields"][name]["exact_checkpoint_equivalent"]
    ]
    row["blocking_fields"] = blockers
    row["exact_checkpoint_ready"] = not blockers
    row["coverage"] = {
        "exact_field_count": categories["EXACT"],
        "partial_field_count": categories["PARTIAL"],
        "missing_field_count": categories["MISSING"],
        "required_field_count": len(checkpoint_v1.REQUIRED_FIELDS),
    }
    return checkpoint_v1.build_manifest(
        rows=[row],
        evidence_manifest={
            "schema": "test_environment_evidence/v1",
            "implementation_revision": "test",
            "content_address": {"sha256": "test-evidence"},
        },
        raw_input={"source": "test strict-prefix snapshot"},
    )


class HistoricalFurySourceBoundPrototypeSmokeRunnerV3Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.prepared = smoke_v2.prepare_source_bound_prototype_wire_smoke_v2(
            bundle_path=smoke_v2.DEFAULT_BUNDLE_MANIFEST,
            model_manifest_path=smoke_v2.DEFAULT_MODEL_MANIFEST,
            dynamic_hypothesis_path=smoke_v2.DEFAULT_DYNAMIC_HYPOTHESIS,
            segment_ref=SEGMENT_REF,
            prototype_id=PROTOTYPE_ID,
            seed=SEED,
        )

    def test_checkpoint_values_not_dynamic_health_hypothesis_feed_registry(self) -> None:
        checkpoint = smoke_v3._derive_prefix_checkpoint_binding_v3(
            self.prepared, _exact_health_manifest()
        )

        baseline = checkpoint.target_health_prefix_registry.targets[0]
        self.assertEqual(999.0, baseline.maximum_health)
        self.assertEqual(777.0, baseline.current_health)
        self.assertNotEqual(
            baseline.current_health,
            self.prepared.prepared_v1.dynamic_config.target_health[0],
        )
        self.assertFalse(
            checkpoint.binding[
                "dynamic_health_hypothesis_consumed_as_prefix_observation"
            ]
        )
        self.assertEqual(
            SEGMENT_REF, checkpoint.binding["selected_row"]["segment_ref"]
        )

    def test_missing_real_exact_health_fails_before_native_bridge(self) -> None:
        with patch.object(smoke_v1, "_verify_windows_v11_bridge_v1") as verify:
            with self.assertRaisesRegex(
                smoke_v3.HistoricalFurySourceBoundPrototypeSmokeRunnerV3Error,
                "target.max_health is not exact strict-prefix checkpoint evidence",
            ):
                smoke_v3.run_local_native_source_bound_prototype_wire_smoke_v3(
                    bundle_path=smoke_v2.DEFAULT_BUNDLE_MANIFEST,
                    model_manifest_path=smoke_v2.DEFAULT_MODEL_MANIFEST,
                    dynamic_hypothesis_path=smoke_v2.DEFAULT_DYNAMIC_HYPOTHESIS,
                    segment_ref=SEGMENT_REF,
                    prototype_id=PROTOTYPE_ID,
                    seed=SEED,
                    checkpoint_manifest_path=smoke_v3.DEFAULT_PREFIX_CHECKPOINT,
                )
        verify.assert_not_called()

    def test_health_hypothesis_label_cannot_masquerade_as_exact_prefix(self) -> None:
        hypothesis = _exact_health_manifest(
            health_source="dynamic_hypothesis.target_health"
        )
        with self.assertRaisesRegex(
            smoke_v3.HistoricalFurySourceBoundPrototypeSmokeRunnerV3Error,
            "not exact strict-prefix checkpoint evidence",
        ):
            smoke_v3._derive_prefix_checkpoint_binding_v3(
                self.prepared, hypothesis
            )

    def test_new_entrypoint_prepares_checkpoint_before_v3_tail(self) -> None:
        checkpoint = smoke_v3._derive_prefix_checkpoint_binding_v3(
            self.prepared, _exact_health_manifest()
        )
        checked = {"checked": "v3"}
        with (
            patch.object(
                smoke_v2,
                "prepare_source_bound_prototype_wire_smoke_v2",
                return_value=self.prepared,
            ) as prepare,
            patch.object(
                smoke_v3,
                "prepare_prefix_checkpoint_binding_v3",
                return_value=checkpoint,
            ) as prepare_checkpoint,
            patch.object(
                smoke_v3,
                "run_prepared_local_native_source_bound_prototype_wire_smoke_v3",
                return_value=checked,
            ) as run_tail,
        ):
            observed = smoke_v3.run_local_native_source_bound_prototype_wire_smoke_v3(
                bundle_path="bundle.json",
                model_manifest_path="model.json",
                dynamic_hypothesis_path="hypothesis.json",
                segment_ref=SEGMENT_REF,
                prototype_id=PROTOTYPE_ID,
                seed=SEED,
                checkpoint_manifest_path="checkpoint.json",
                bridge_path="bridge.exe",
                simulator_root="sim-root",
                max_decisions=123,
                max_advances=456,
            )

        self.assertIs(checked, observed)
        prepare.assert_called_once()
        prepare_checkpoint.assert_called_once_with(
            self.prepared, "checkpoint.json"
        )
        run_tail.assert_called_once_with(
            self.prepared,
            checkpoint=checkpoint,
            bridge_path="bridge.exe",
            simulator_root="sim-root",
            max_decisions=123,
            max_advances=456,
        )


if __name__ == "__main__":
    unittest.main()
