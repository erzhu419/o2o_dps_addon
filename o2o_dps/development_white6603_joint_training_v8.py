"""Development-only one-pass v7 joint counts plus v8 white opportunities.

This is a training artifact, not a runtime sampler or a formal v7 worker.
Call ``begin_wave`` before each Stage-5 sufficient-row iterator.
"""

from __future__ import annotations

from collections import Counter
import json
from typing import Any, Mapping

from . import chronicle_external_teammate_response_hpc_v1 as hpc_v1
from .development_white6603_opportunity_v8 import white6603_opportunity_from_prefix_v8


SCHEMA = "development_white6603_joint_training/v8"
WHITE_TOKEN = json.dumps(
    ["DMG", 6603, "DIRECT_FRIENDLY_PLAYER"], separators=(",", ":")
)


class JointWhiteTrainingV8:
    """Accumulate both heads while visiting each sufficient row once."""

    def __init__(self) -> None:
        self.joint = hpc_v1._JointTrainingCountsV4()
        self.white_counts: dict[tuple[Any, ...], Counter[bool]] = {}
        self.wave_count = 0
        self.in_wave = False

    def begin_wave(self) -> None:
        self.in_wave = True
        self.wave_count += 1

    def update(self, row: Mapping[str, Any]) -> dict[str, Any]:
        if not self.in_wave:
            raise ValueError("begin_wave must precede the first sufficient row")
        observation = white6603_opportunity_from_prefix_v8(row)
        self.joint.update(row)
        for context in observation["contexts"]:
            self.white_counts.setdefault(context, Counter())[observation["white6603"]] += 1
        return observation

    @property
    def row_count(self) -> int:
        return self.joint.row_count

    @property
    def white_count(self) -> int:
        return sum(
            counts[True]
            for context, counts in self.white_counts.items()
            if context[0] == "GLOBAL"
        )

    def serialize(self) -> dict[str, Any]:
        global_rows = sum(
            sum(counts.values())
            for context, counts in self.white_counts.items()
            if context[0] == "GLOBAL"
        )
        if global_rows != self.row_count:
            raise ValueError("white opportunity GLOBAL rows differ from joint rows")
        joint_white = self.joint.base_c.mark_counts[("GLOBAL",)][WHITE_TOKEN]
        if joint_white != self.white_count:
            raise ValueError("white opportunity positives differ from joint mark count")
        rows = [
            {"context": list(context), "white": counts[True], "nonwhite": counts[False]}
            for context, counts in self.white_counts.items()
        ]
        rows.sort(key=lambda row: json.dumps(row["context"], separators=(",", ":")))
        return {
            "schema": SCHEMA,
            "wave_count": self.wave_count,
            "row_count": self.row_count,
            "white_count": self.white_count,
            "joint_dynamic_training_counts": hpc_v1.serialize_joint_training_v4(self.joint),
            "white_opportunity_counts": rows,
        }

    @classmethod
    def deserialize(cls, value: Mapping[str, Any]) -> "JointWhiteTrainingV8":
        if value.get("schema") != SCHEMA:
            raise ValueError("unsupported white joint training schema")
        result = cls()
        result.joint = hpc_v1.deserialize_joint_training_v4(
            value["joint_dynamic_training_counts"]
        )
        result.wave_count = value["wave_count"]
        for row in value["white_opportunity_counts"]:
            context = tuple(row["context"])
            result.white_counts[context] = Counter(
                {True: row["white"], False: row["nonwhite"]}
            )
        if (
            result.row_count != value["row_count"]
            or result.white_count != value["white_count"]
        ):
            raise ValueError("white joint training count differs on load")
        return result
