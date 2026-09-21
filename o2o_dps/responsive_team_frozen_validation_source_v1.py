"""Bind a development replay to one frozen Formal D validation partition.

This checks the dispatch's recorded source identity; it does not re-hash the
large Stage-5 partition. The worker/reducer establish that file identity.
"""

from __future__ import annotations

from typing import Any, Mapping


def verify_frozen_validation_source_v1(
    dispatch: Mapping[str, Any],
    *,
    instance_id: str,
    component_id: str,
    partition_locator: str,
    stage5_content_sha256: str,
    partition_compressed_file_sha256: str,
) -> dict[str, str]:
    """Return the exact validated source binding or raise ``ValueError``."""

    tasks = dispatch["tasks"]
    matches = [task for task in tasks if task["instance_id"] == instance_id]
    if len(matches) != 1:
        raise ValueError("source instance must identify exactly one dispatch task")
    task = matches[0]
    if task["split"] != "VALIDATION":
        raise ValueError("source instance is not a VALIDATION task")
    validation_components = dispatch["split_contract"]["validation_component_ids"]
    if (
        task["component_id"] != component_id
        or component_id not in validation_components
        or any(
            row["component_id"] == component_id and row["split"] != "VALIDATION"
            for row in tasks
        )
    ):
        raise ValueError("source component is not a frozen validation component")
    if task["partition_locator"] != partition_locator:
        raise ValueError("source partition locator differs from dispatch")
    if dispatch["source_bindings"]["stage5"]["content_sha256"] != stage5_content_sha256:
        raise ValueError("Stage-5 source identity differs from dispatch")
    if task["partition"]["compressed_file_sha256"] != partition_compressed_file_sha256:
        raise ValueError("source partition identity differs from dispatch")
    return {
        "instance_id": instance_id,
        "component_id": component_id,
        "partition_locator": partition_locator,
        "stage5_content_sha256": stage5_content_sha256,
        "partition_compressed_file_sha256": partition_compressed_file_sha256,
        "instance_entry_content_sha256": task["instance_entry_content_sha256"],
    }
